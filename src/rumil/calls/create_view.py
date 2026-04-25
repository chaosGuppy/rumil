"""Create View call: synthesize P1 output into a structured View page."""

import logging
import uuid
from collections.abc import Sequence

from rumil.calls.closing_reviewers import ViewClosingReview
from rumil.calls.context_builders import CreateViewContext
from rumil.calls.page_creators import SimpleAgentLoop
from rumil.calls.stages import (
    CallInfra,
    CallRunner,
    ClosingReviewer,
    ContextBuilder,
    ContextResult,
    UpdateResult,
    WorkspaceUpdater,
)
from rumil.constants import DEFAULT_VIEW_SECTIONS
from rumil.database import DB
from rumil.models import (
    Call,
    CallType,
    LinkType,
    MoveType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)
from rumil.settings import get_settings
from rumil.tracing.trace_events import ViewCreatedEvent

log = logging.getLogger(__name__)


class _CreateViewUpdater(WorkspaceUpdater):
    """Workspace updater for CREATE_VIEW: materializes the view page, then
    delegates to a standard ``SimpleAgentLoop`` for tool-driven item creation.

    Materialization runs at the top of ``update_workspace`` so
    ``--up-to-stage build_context`` doesn't persist anything — the view page
    is side-effect-free until the LLM stage actually starts.
    """

    def __init__(
        self,
        view_id: str,
        call_type: CallType,
        task_description: str,
        available_moves: Sequence[MoveType] | None,
    ) -> None:
        self._view_id = view_id
        self._call_type = call_type
        self._inner = SimpleAgentLoop(
            call_type,
            task_description,
            available_moves=available_moves,
        )

    async def materialize(self, infra: CallInfra) -> str | None:
        """Persist the View page + VIEW_OF link, superseding any prior view
        for this question. Returns the superseded view id (or None).
        Exposed for tests.
        """
        db = infra.db
        question_id = infra.question_id
        existing_view = await db.get_view_for_question(question_id)

        question = await db.get_page(question_id)
        q_headline = question.headline if question else question_id[:8]

        view = Page(
            id=self._view_id,
            page_type=PageType.VIEW,
            layer=PageLayer.WIKI,
            workspace=Workspace.RESEARCH,
            content="",
            headline=f"View: {q_headline}",
            sections=list(DEFAULT_VIEW_SECTIONS),
            provenance_call_type=self._call_type.value,
            provenance_call_id=infra.call.id,
            provenance_model=get_settings().model,
        )
        await db.save_page(view)

        await db.save_link(
            PageLink(
                from_page_id=view.id,
                to_page_id=question_id,
                link_type=LinkType.VIEW_OF,
            )
        )

        if existing_view:
            await db.supersede_page(existing_view.id, view.id)
            log.info(
                "Superseded old view %s with new view %s",
                existing_view.id[:8],
                view.id[:8],
            )

        log.info(
            "Created view page %s for question %s",
            view.id[:8],
            question_id[:8],
        )
        await infra.trace.record_strict(
            ViewCreatedEvent(
                view_id=view.id,
                view_headline=view.headline,
                question_id=question_id,
                superseded_view_id=existing_view.id if existing_view else None,
            )
        )
        return existing_view.id if existing_view else None

    async def update_workspace(
        self,
        infra: CallInfra,
        context: ContextResult,
    ) -> UpdateResult:
        await self.materialize(infra)
        return await self._inner.update_workspace(infra, context)


class CreateViewCall(CallRunner):
    """Create a View page for a question from available evidence."""

    context_builder_cls = CreateViewContext
    workspace_updater_cls = SimpleAgentLoop
    closing_reviewer_cls = ViewClosingReview  # type: ignore[assignment]
    call_type = CallType.CREATE_VIEW

    def __init__(self, question_id: str, call: Call, db: DB, **kwargs) -> None:
        # Mint the view UUID up front so factories (task_description, closing
        # reviewer, updater) can bind to it without needing the view to exist
        # in the DB yet. The actual save_page happens at update_workspace time
        # so --up-to-stage build_context is side-effect-free.
        self._view_id: str = str(uuid.uuid4())
        super().__init__(question_id, call, db, **kwargs)

    def _make_context_builder(self) -> ContextBuilder:
        return CreateViewContext()

    def _make_workspace_updater(self) -> WorkspaceUpdater:
        return _CreateViewUpdater(
            view_id=self._view_id,
            call_type=self.call_type,
            task_description=self.task_description(),
            available_moves=self._resolve_available_moves(),
        )

    def _make_closing_reviewer(self) -> ClosingReviewer:
        return ViewClosingReview(
            self.call_type,
            view_id=self._view_id,
        )

    def task_description(self) -> str:
        settings = get_settings()
        sections_list = ", ".join(DEFAULT_VIEW_SECTIONS)
        return (
            "Create a View page for this question.\n\n"
            f"Question ID: `{self.infra.question_id}`\n"
            f"View ID: `{self._view_id}`\n\n"
            "Survey the available evidence and create atomic View items, "
            "each assigned to a section with robustness and importance scores.\n\n"
            f"**Available sections:** {sections_list}\n\n"
            "**Importance caps:**\n"
            f"- Importance 5: max {settings.view_importance_5_cap} items\n"
            f"- Importance 4: max {settings.view_importance_4_cap} items\n"
            f"- Importance 3: max {settings.view_importance_3_cap} items\n"
            f"- Importance 2: max {settings.view_importance_2_cap} items\n"
            f"- Importance 1: no cap\n\n"
            "Use the `create_view_item` tool for each item. Pass the View ID shown above "
            "as `view_id` in each tool call.\n\n"
            "Prioritize quality over quantity. A focused View with 10-20 well-scored items "
            "is better than a sprawling one with 50 marginal items."
        )
