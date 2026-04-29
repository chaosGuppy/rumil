"""Create / update FreeformView calls.

A FreeformView is a four-section prose summary of a question, written one
section at a time in a shared, cached conversation at maximum reasoning effort.
Reuses the existing VIEW / VIEW_ITEM page machinery: one VIEW page with
``sections=FREEFORM_VIEW_SECTIONS`` and ``extra={"view_kind": "freeform"}``,
plus four VIEW_ITEM pages (one per section) linked via VIEW_ITEM with
``section`` and ``position=0``.
"""

import logging
import uuid
from collections.abc import Mapping

from rumil.calls.closing_reviewers import StandardClosingReview
from rumil.calls.context_builders import FreeformViewContext
from rumil.calls.stages import (
    CallInfra,
    CallRunner,
    ClosingReviewer,
    ContextBuilder,
    ContextResult,
    UpdateResult,
    WorkspaceUpdater,
)
from rumil.constants import FREEFORM_VIEW_SECTION_BRIEFS, FREEFORM_VIEW_SECTIONS
from rumil.database import DB
from rumil.llm import (
    LLMExchangeMetadata,
    build_system_prompt,
    text_call,
)
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

VIEW_KIND_FREEFORM = "freeform"


async def _run_section_sequence(
    infra: CallInfra,
    context: ContextResult,
    *,
    view_id: str,
    prior_sections: Mapping[str, str] | None = None,
) -> UpdateResult:
    """Run one max-effort LLM call per FreeformView section, sharing message
    history with prompt caching across calls. Persists each section as a
    VIEW_ITEM page linked to ``view_id``. Returns the accumulated UpdateResult.
    """
    system_prompt = build_system_prompt("freeform_view")
    messages: list[dict] = []
    created_page_ids: list[str] = []

    for i, section_name in enumerate(FREEFORM_VIEW_SECTIONS):
        parts: list[str] = []
        if i == 0:
            parts.append(context.context_text)
        parts.append(FREEFORM_VIEW_SECTION_BRIEFS[section_name])
        if prior_sections and section_name in prior_sections:
            parts.append(
                "Here is the previous version of this section. Write a fresh, "
                "standalone replacement that incorporates new findings; do not "
                "reference, echo, or compare against this prior version.\n\n"
                "<prior_version>\n" + prior_sections[section_name] + "\n</prior_version>"
            )
        user_msg = "\n\n".join(parts)
        messages.append({"role": "user", "content": user_msg})

        meta = LLMExchangeMetadata(
            call_id=infra.call.id,
            phase=f"freeform_section_{section_name}",
        )
        response_text = await text_call(
            system_prompt,
            messages=messages,
            cache=True,
            effort="max",
            metadata=meta,
            db=infra.db,
        )
        messages.append({"role": "assistant", "content": response_text})

        item = Page(
            page_type=PageType.VIEW_ITEM,
            layer=PageLayer.WIKI,
            workspace=Workspace.RESEARCH,
            headline=section_name,
            content=response_text,
            provenance_call_type=infra.call.call_type.value,
            provenance_call_id=infra.call.id,
            provenance_model=get_settings().model,
        )
        await infra.db.save_page(item)
        await infra.db.save_link(
            PageLink(
                from_page_id=view_id,
                to_page_id=item.id,
                link_type=LinkType.VIEW_ITEM,
                section=section_name,
                position=0,
            )
        )
        created_page_ids.append(item.id)
        log.info(
            "FreeformView section %s -> page %s (%d chars)",
            section_name,
            item.id[:8],
            len(response_text),
        )

    return UpdateResult(
        created_page_ids=created_page_ids,
        moves=[],
        all_loaded_ids=[],
        messages=messages,
    )


class _CreateFreeformViewUpdater(WorkspaceUpdater):
    """Materializes a fresh FreeformView page, then runs the four-section
    LLM sequence. Rejects creation if a view already exists for the question
    — callers go through ``FreeformView.refresh()`` for updates."""

    def __init__(self, view_id: str, call_type: CallType) -> None:
        self._view_id = view_id
        self._call_type = call_type

    async def materialize(self, infra: CallInfra) -> None:
        db = infra.db
        question_id = infra.question_id
        existing_view = await db.get_view_for_question(question_id)
        if existing_view:
            raise ValueError(
                f"CreateFreeformViewCall on question {question_id[:8]} but a view "
                f"already exists ({existing_view.id[:8]}). Use UpdateFreeformViewCall "
                f"or FreeformView.refresh()."
            )

        question = await db.get_page(question_id)
        q_headline = question.headline if question else question_id[:8]

        view = Page(
            id=self._view_id,
            page_type=PageType.VIEW,
            layer=PageLayer.WIKI,
            workspace=Workspace.RESEARCH,
            content="",
            headline=f"View: {q_headline}",
            sections=list(FREEFORM_VIEW_SECTIONS),
            extra={"view_kind": VIEW_KIND_FREEFORM},
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
        await infra.trace.record_strict(
            ViewCreatedEvent(
                view_id=view.id,
                view_headline=view.headline,
                question_id=question_id,
                superseded_view_id=None,
            )
        )

    async def update_workspace(
        self,
        infra: CallInfra,
        context: ContextResult,
    ) -> UpdateResult:
        await self.materialize(infra)
        return await _run_section_sequence(
            infra,
            context,
            view_id=self._view_id,
            prior_sections=None,
        )


class _UpdateFreeformViewUpdater(WorkspaceUpdater):
    """Mints a new FreeformView page (superseding the old one), reads the
    prior section content, and runs the four-section LLM sequence with the
    prior version of each section fed into its user message.

    Unlike ``UpdateViewWorkspaceUpdater``, we do not copy old VIEW_ITEM links
    onto the new view: a freeform update always rewrites all four sections,
    so four fresh items are created on the new view and the old items remain
    on the superseded view for audit.
    """

    def __init__(self, view_id: str, call_type: CallType) -> None:
        self._view_id = view_id
        self._call_type = call_type

    async def materialize(self, infra: CallInfra) -> tuple[str, dict[str, str]]:
        """Create a new VIEW page, supersede the old, and return
        ``(old_view_id, prior_sections_by_name)``."""
        db = infra.db
        question_id = infra.question_id
        existing_view = await db.get_view_for_question(question_id)
        if existing_view is None:
            raise RuntimeError(
                f"UpdateFreeformViewCall requires an existing View for question "
                f"{question_id[:8]}, but none was found."
            )

        question = await db.get_page(question_id)
        q_headline = question.headline if question else question_id[:8]

        new_view = Page(
            id=self._view_id,
            page_type=PageType.VIEW,
            layer=PageLayer.WIKI,
            workspace=Workspace.RESEARCH,
            content="",
            headline=f"View: {q_headline}",
            sections=list(FREEFORM_VIEW_SECTIONS),
            extra={"view_kind": VIEW_KIND_FREEFORM},
            provenance_call_type=self._call_type.value,
            provenance_call_id=infra.call.id,
            provenance_model=get_settings().model,
        )
        await db.save_page(new_view)
        await db.save_link(
            PageLink(
                from_page_id=new_view.id,
                to_page_id=question_id,
                link_type=LinkType.VIEW_OF,
            )
        )
        await db.supersede_page(existing_view.id, new_view.id)

        prior_items = await db.get_view_items(existing_view.id)
        prior_sections: dict[str, str] = {}
        for page, link in prior_items:
            if link.section and page.content:
                prior_sections[link.section] = page.content

        await infra.trace.record_strict(
            ViewCreatedEvent(
                view_id=new_view.id,
                view_headline=new_view.headline,
                question_id=question_id,
                superseded_view_id=existing_view.id,
            )
        )
        log.info(
            "FreeformView update: new view %s supersedes %s; %d prior sections recovered",
            new_view.id[:8],
            existing_view.id[:8],
            len(prior_sections),
        )
        return existing_view.id, prior_sections

    async def update_workspace(
        self,
        infra: CallInfra,
        context: ContextResult,
    ) -> UpdateResult:
        _, prior_sections = await self.materialize(infra)
        return await _run_section_sequence(
            infra,
            context,
            view_id=self._view_id,
            prior_sections=prior_sections,
        )


class CreateFreeformViewCall(CallRunner):
    """Create a FreeformView for a question."""

    context_builder_cls = FreeformViewContext
    workspace_updater_cls = _CreateFreeformViewUpdater  # type: ignore[assignment]
    closing_reviewer_cls = StandardClosingReview  # type: ignore[assignment]
    call_type = CallType.CREATE_FREEFORM_VIEW

    def __init__(self, question_id: str, call: Call, db: DB, **kwargs) -> None:
        self._view_id: str = str(uuid.uuid4())
        super().__init__(question_id, call, db, **kwargs)

    def _make_context_builder(self) -> ContextBuilder:
        return FreeformViewContext()

    def _make_workspace_updater(self) -> WorkspaceUpdater:
        return _CreateFreeformViewUpdater(view_id=self._view_id, call_type=self.call_type)

    def _make_closing_reviewer(self) -> ClosingReviewer:
        return StandardClosingReview(self.call_type)

    def task_description(self) -> str:
        return f"Create a FreeformView for question `{self.infra.question_id}`."


class UpdateFreeformViewCall(CallRunner):
    """Update an existing FreeformView for a question by superseding it."""

    context_builder_cls = FreeformViewContext
    workspace_updater_cls = _UpdateFreeformViewUpdater  # type: ignore[assignment]
    closing_reviewer_cls = StandardClosingReview  # type: ignore[assignment]
    call_type = CallType.UPDATE_FREEFORM_VIEW

    def __init__(self, question_id: str, call: Call, db: DB, **kwargs) -> None:
        self._view_id: str = str(uuid.uuid4())
        super().__init__(question_id, call, db, **kwargs)

    def _make_context_builder(self) -> ContextBuilder:
        return FreeformViewContext()

    def _make_workspace_updater(self) -> WorkspaceUpdater:
        return _UpdateFreeformViewUpdater(view_id=self._view_id, call_type=self.call_type)

    def _make_closing_reviewer(self) -> ClosingReviewer:
        return StandardClosingReview(self.call_type)

    def task_description(self) -> str:
        return f"Update the FreeformView for question `{self.infra.question_id}`."
