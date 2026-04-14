"""Create View call: synthesize P1 output into a structured View page."""

import logging

from rumil.calls.closing_reviewers import ViewClosingReview
from rumil.calls.context_builders import CreateViewContext
from rumil.calls.page_creators import SimpleAgentLoop
from rumil.calls.stages import (
    CallRunner,
    ClosingReviewer,
    ContextBuilder,
    WorkspaceUpdater,
)
from rumil.constants import DEFAULT_VIEW_SECTIONS
from rumil.database import DB
from rumil.models import (
    Call,
    CallType,
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)
from rumil.settings import get_settings
from rumil.tracing.trace_events import ViewCreatedEvent

log = logging.getLogger(__name__)


class CreateViewCall(CallRunner):
    """Create a View page for a question from available evidence."""

    context_builder_cls = CreateViewContext
    workspace_updater_cls = SimpleAgentLoop
    closing_reviewer_cls = ViewClosingReview  # type: ignore[assignment]
    call_type = CallType.CREATE_VIEW

    def __init__(self, question_id: str, call: Call, db: DB, **kwargs) -> None:
        self._view_id: str = ""
        super().__init__(question_id, call, db, **kwargs)

    async def _run_stages(self) -> None:
        """Create the View page infrastructure, then rebuild stages that need the view_id."""
        self._view_id = await self._create_view_page()
        self.workspace_updater = self._make_workspace_updater()
        self.closing_reviewer = self._make_closing_reviewer()
        await super()._run_stages()

    async def _create_view_page(self) -> str:
        """Create the View page and VIEW_OF link before the LLM starts."""
        existing_view = await self.infra.db.get_view_for_question(
            self.infra.question_id
        )

        question = await self.infra.db.get_page(self.infra.question_id)
        q_headline = question.headline if question else self.infra.question_id[:8]

        view = Page(
            page_type=PageType.VIEW,
            layer=PageLayer.WIKI,
            workspace=Workspace.RESEARCH,
            content="",
            headline=f"View: {q_headline}",
            sections=list(DEFAULT_VIEW_SECTIONS),
            provenance_call_type=self.call_type.value,
            provenance_call_id=self.infra.call.id,
            provenance_model=get_settings().model,
        )
        await self.infra.db.save_page(view)

        await self.infra.db.save_link(
            PageLink(
                from_page_id=view.id,
                to_page_id=self.infra.question_id,
                link_type=LinkType.VIEW_OF,
            )
        )

        if existing_view:
            await self.infra.db.supersede_page(existing_view.id, view.id)
            log.info(
                "Superseded old view %s with new view %s",
                existing_view.id[:8],
                view.id[:8],
            )

        log.info(
            "Created view page %s for question %s",
            view.id[:8],
            self.infra.question_id[:8],
        )
        await self.infra.trace.record_strict(
            ViewCreatedEvent(
                view_id=view.id,
                view_headline=view.headline,
                question_id=self.infra.question_id,
                superseded_view_id=existing_view.id if existing_view else None,
            )
        )
        return view.id

    def _make_context_builder(self) -> ContextBuilder:
        return CreateViewContext()

    def _make_workspace_updater(self) -> WorkspaceUpdater:
        return SimpleAgentLoop(
            self.call_type,
            self.task_description(),
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
            "each assigned to a section with credence, robustness, and importance scores.\n\n"
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
