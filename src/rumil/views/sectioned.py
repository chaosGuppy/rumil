"""`SectionedView`: View stored as a View page with sectioned, importance-scored items.

This is the default variant. It wraps the existing CreateViewCall /
UpdateViewCall lifecycle and the `render_view` renderer.
"""

import logging
from collections.abc import Sequence
from datetime import datetime

from rumil.budget import _consume_budget
from rumil.calls.create_view import CreateViewCall
from rumil.calls.update_view import UpdateViewCall
from rumil.context import format_page, render_view
from rumil.database import DB
from rumil.models import CallType, PageDetail
from rumil.tracing.broadcast import Broadcaster
from rumil.views.base import View

log = logging.getLogger(__name__)


class SectionedView(View):
    """View as a sectioned, importance-capped, items-based page (PageType.VIEW)."""

    async def exists(self, question_id: str, db: DB) -> bool:
        return await db.get_view_for_question(question_id) is not None

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
            return await update_view_for_question(
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
        return await create_view_for_question(
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
        return await self._render_view(question_id, db, min_importance=2)

    async def render_for_parent_scoring(self, question_id: str, db: DB) -> str | None:
        return await self._render_view(question_id, db, min_importance=2)

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
        lines = [f"**Status:** View available{' [NEW]' if is_new else ''}"]

        if view.content:
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

        if is_new:
            items = await db.get_view_items(view.id, min_importance=4)
            if items:
                lines.append("")
                lines.append("**Key items:**")
                for page, link in items:
                    imp = link.importance or 0
                    r = page.robustness if page.robustness is not None else "?"
                    lines.append(f"- [R{r} I{imp}] `{page.id[:8]}` — {page.headline}")
                    page_ids.append(page.id)

        return is_new, "\n".join(lines), page_ids

    async def _render_view(
        self,
        question_id: str,
        db: DB,
        *,
        min_importance: int,
    ) -> str | None:
        view = await db.get_view_for_question(question_id)
        if view is None:
            return None
        items = await db.get_view_items(view.id, min_importance=min_importance)
        text = await render_view(view, items, min_importance=min_importance)
        return text if text.strip() else None


async def create_view_for_question(
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
    """Run a CreateView call. Returns call ID, or None if no budget."""
    log.info("create_view_for_question: question=%s", question_id[:8])
    if not await _consume_budget(db, force=force, pool_question_id=pool_question_id):
        return None

    call = await db.create_call(
        CallType.CREATE_VIEW,
        scope_page_id=question_id,
        parent_call_id=parent_call_id,
        context_page_ids=context_page_ids,
        call_id=call_id,
        sequence_id=sequence_id,
        sequence_position=sequence_position,
    )
    instance = CreateViewCall(question_id, call, db, broadcaster=broadcaster)
    await instance.run()
    return call.id


async def update_view_for_question(
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
    """Run an UpdateView call. Returns call ID, or None if no budget."""
    log.info("update_view_for_question: question=%s", question_id[:8])
    if not await _consume_budget(db, force=force, pool_question_id=pool_question_id):
        return None

    call = await db.create_call(
        CallType.UPDATE_VIEW,
        scope_page_id=question_id,
        parent_call_id=parent_call_id,
        context_page_ids=context_page_ids,
        call_id=call_id,
        sequence_id=sequence_id,
        sequence_position=sequence_position,
    )
    instance = UpdateViewCall(question_id, call, db, broadcaster=broadcaster)
    await instance.run()
    return call.id
