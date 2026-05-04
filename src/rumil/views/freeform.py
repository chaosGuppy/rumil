"""`FreeformView`: View stored as a VIEW page with four long-form prose sections.

Each section is a separate VIEW_ITEM page linked via VIEW_ITEM with a
``section`` name; the VIEW page carries ``extra={"view_kind": "freeform"}``
so callers can distinguish from sectioned views.
"""

import logging
from collections.abc import Sequence
from datetime import datetime

from rumil.budget import _consume_budget
from rumil.calls.freeform_view import (
    CreateFreeformViewCall,
    UpdateFreeformViewCall,
)
from rumil.context import format_page, render_freeform_view
from rumil.database import DB
from rumil.models import CallType, Page, PageDetail
from rumil.tracing.broadcast import Broadcaster
from rumil.views.base import View

log = logging.getLogger(__name__)


class FreeformView(View):
    """View as a four-section, long-form-prose VIEW page."""

    async def exists(self, question_id: str, db: DB) -> bool:
        return await db.get_view_for_question(question_id) is not None

    async def headline_page(self, question_id: str, db: DB) -> Page | None:
        return await db.get_view_for_question(question_id)

    async def headline_pages_many(
        self,
        question_ids: Sequence[str],
        db: DB,
    ) -> dict[str, Page | None]:
        return await db.get_views_for_questions(question_ids)

    async def render_for_executive_summary(
        self,
        question_id: str,
        db: DB,
    ) -> str | None:
        view = await db.get_view_for_question(question_id)
        if view is None:
            return None
        items = await db.get_view_items(view.id)
        text = await render_freeform_view(view, items)
        return text if text.strip() else None

    async def refresh(
        self,
        question_id: str,
        db: DB,
        *,
        parent_call_id: str | None = None,
        context_page_ids: Sequence[str] | None = None,
        broadcaster: Broadcaster | None = None,
        force: bool = False,
        call_id: str | None = None,
        sequence_id: str | None = None,
        sequence_position: int | None = None,
        pool_question_id: str | None = None,
    ) -> str | None:
        if await self.exists(question_id, db):
            return await update_freeform_view_for_question(
                question_id,
                db,
                parent_call_id=parent_call_id,
                context_page_ids=context_page_ids,
                broadcaster=broadcaster,
                force=force,
                call_id=call_id,
                sequence_id=sequence_id,
                sequence_position=sequence_position,
                pool_question_id=pool_question_id,
            )
        return await create_freeform_view_for_question(
            question_id,
            db,
            parent_call_id=parent_call_id,
            context_page_ids=context_page_ids,
            broadcaster=broadcaster,
            force=force,
            call_id=call_id,
            sequence_id=sequence_id,
            sequence_position=sequence_position,
            pool_question_id=pool_question_id,
        )

    async def render_for_prioritization(self, question_id: str, db: DB) -> str | None:
        return await self.render_for_executive_summary(question_id, db)

    async def render_for_parent_scoring(self, question_id: str, db: DB) -> str | None:
        return await self.render_for_executive_summary(question_id, db)

    async def render_for_scout(self, question_id: str, db: DB) -> str | None:
        body = await self.render_for_executive_summary(question_id, db)
        if body is None:
            return None
        callout = (
            "### Notes for scouts\n\n"
            "The **research_direction** section above is the canonical source "
            "of subquestion-worthy cruxes — its job is precisely to surface "
            "the unknowns that, if investigated, would most improve the "
            "answer. Read it before deciding what to scout. "
            "**returns_to_further_research** is also useful supporting "
            "context for sizing scout effort."
        )
        return f"## Current view on this question\n\n{body}\n\n{callout}"

    async def render_for_child_investigation_results(
        self,
        question_id: str,
        db: DB,
        *,
        last_view_created_at: datetime | None,
    ) -> tuple[bool, str, list[str]] | None:
        view = await db.get_view_for_question(question_id)
        if view is None:
            return None

        is_new = last_view_created_at is None or view.created_at > last_view_created_at
        page_ids: list[str] = [view.id]
        lines = [f"**Status:** FreeformView available{' [NEW]' if is_new else ''}"]

        items = await db.get_view_items(view.id)
        if items:
            text = await render_freeform_view(view, items)
            if text.strip():
                lines.append("")
                lines.append(text)
            for page, _link in items:
                page_ids.append(page.id)
        elif view.content:
            detail = PageDetail.CONTENT if is_new else PageDetail.ABSTRACT
            lines.append("")
            lines.append(
                await format_page(
                    view,
                    detail,
                    linked_detail=None,
                    db=db,
                    track=True,
                    track_tags={"source": "child_investigation"},
                )
            )

        return is_new, "\n".join(lines), page_ids


async def create_freeform_view_for_question(
    question_id: str,
    db: DB,
    *,
    parent_call_id: str | None = None,
    context_page_ids: Sequence[str] | None = None,
    broadcaster: Broadcaster | None = None,
    force: bool = False,
    call_id: str | None = None,
    sequence_id: str | None = None,
    sequence_position: int | None = None,
    pool_question_id: str | None = None,
) -> str | None:
    """Run a CreateFreeformView call. Returns call ID, or None if no budget."""
    log.info("create_freeform_view_for_question: question=%s", question_id[:8])
    if not await _consume_budget(db, force=force, pool_question_id=pool_question_id):
        return None

    call = await db.create_call(
        CallType.CREATE_FREEFORM_VIEW,
        scope_page_id=question_id,
        parent_call_id=parent_call_id,
        context_page_ids=context_page_ids,
        call_id=call_id,
        sequence_id=sequence_id,
        sequence_position=sequence_position,
    )
    instance = CreateFreeformViewCall(question_id, call, db, broadcaster=broadcaster)
    await instance.run()
    return call.id


async def update_freeform_view_for_question(
    question_id: str,
    db: DB,
    *,
    parent_call_id: str | None = None,
    context_page_ids: Sequence[str] | None = None,
    broadcaster: Broadcaster | None = None,
    force: bool = False,
    call_id: str | None = None,
    sequence_id: str | None = None,
    sequence_position: int | None = None,
    pool_question_id: str | None = None,
) -> str | None:
    """Run an UpdateFreeformView call. Returns call ID, or None if no budget."""
    log.info("update_freeform_view_for_question: question=%s", question_id[:8])
    if not await _consume_budget(db, force=force, pool_question_id=pool_question_id):
        return None

    call = await db.create_call(
        CallType.UPDATE_FREEFORM_VIEW,
        scope_page_id=question_id,
        parent_call_id=parent_call_id,
        context_page_ids=context_page_ids,
        call_id=call_id,
        sequence_id=sequence_id,
        sequence_position=sequence_position,
    )
    instance = UpdateFreeformViewCall(question_id, call, db, broadcaster=broadcaster)
    await instance.run()
    return call.id
