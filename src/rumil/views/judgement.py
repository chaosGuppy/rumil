"""`JudgementView`: View stored as a flat NL judgement page.

Wraps `assess_question(..., summarise=False)` and the judgement-rendering
helpers. Demonstrates that "views are just judgements" is a valid concrete
implementation of the abstract View concept.
"""

import logging
from collections.abc import Sequence
from datetime import datetime

from rumil.context import format_page
from rumil.database import DB
from rumil.models import Page, PageDetail
from rumil.orchestrators.common import assess_question
from rumil.tracing.broadcast import Broadcaster
from rumil.views.base import View

log = logging.getLogger(__name__)


class JudgementView(View):
    """View as a flat NL judgement — each refresh appends a new judgement page."""

    async def exists(self, question_id: str, db: DB) -> bool:
        judgements = await db.get_judgements_for_question(question_id)
        return bool(judgements)

    async def headline_page(self, question_id: str, db: DB) -> Page | None:
        return await self._latest_judgement(question_id, db)

    async def headline_pages_many(
        self,
        question_ids: Sequence[str],
        db: DB,
    ) -> dict[str, Page | None]:
        by_question = await db.get_judgements_for_questions(question_ids)
        return {
            qid: max(judgements, key=lambda j: j.created_at) if judgements else None
            for qid, judgements in by_question.items()
        }

    async def render_for_executive_summary(
        self,
        question_id: str,
        db: DB,
    ) -> str | None:
        latest = await self._latest_judgement(question_id, db)
        if latest is None:
            return None
        return await format_page(
            latest,
            PageDetail.CONTENT,
            linked_detail=None,
            db=db,
        )

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
        return await assess_question(
            question_id,
            db,
            parent_call_id=parent_call_id,
            context_page_ids=list(context_page_ids) if context_page_ids else None,
            broadcaster=broadcaster,
            force=force,
            call_id=call_id,
            sequence_id=sequence_id,
            sequence_position=sequence_position,
            summarise=False,
            pool_question_id=pool_question_id,
        )

    async def render_for_prioritization(self, question_id: str, db: DB) -> str | None:
        latest = await self._latest_judgement(question_id, db)
        if latest is None:
            return None
        lines = [f"## Current Judgement: {latest.headline}", ""]
        if latest.credence is not None:
            lines.append(f"Credence: {latest.credence}/9")
        if latest.robustness is not None:
            lines.append(f"Robustness: {latest.robustness}/5")
        if latest.abstract:
            lines.append("")
            lines.append(latest.abstract)
        return "\n".join(lines)

    async def render_for_parent_scoring(self, question_id: str, db: DB) -> str | None:
        latest = await self._latest_judgement(question_id, db)
        if latest is None:
            return None
        parts = [f"Latest judgement (robustness {latest.robustness}/5):"]
        parts.append(latest.abstract or latest.headline)
        return "\n".join(parts)

    async def render_for_scout(self, question_id: str, db: DB) -> str | None:
        body = await self.render_for_prioritization(question_id, db)
        if body is None:
            return None
        callout = (
            "### Notes for scouts\n\n"
            "This is the workspace's standing answer. Use it to identify "
            "what still needs examining — places where the judgement leans "
            "on assumptions, where confidence is low, or where the reasoning "
            "would change if a key consideration moved. Generate subquestions "
            "that target those leverage points."
        )
        return f"## Current take on this question\n\n{body}\n\n{callout}"

    async def render_for_child_investigation_results(
        self,
        question_id: str,
        db: DB,
        *,
        last_view_created_at: datetime | None,
    ) -> tuple[bool, str, list[str]] | None:
        latest = await self._latest_judgement(question_id, db)
        if latest is None:
            return None

        is_new = last_view_created_at is None or latest.created_at > last_view_created_at
        detail = PageDetail.CONTENT if is_new else PageDetail.ABSTRACT
        lines = [f"**Status:** Judgement available{' [NEW]' if is_new else ''}", ""]
        lines.append(
            await format_page(
                latest,
                detail,
                linked_detail=None,
                db=db,
                track=True,
                track_tags={"source": "child_investigation"},
            )
        )
        return is_new, "\n".join(lines), [latest.id]

    async def _latest_judgement(self, question_id: str, db: DB):
        judgements = await db.get_judgements_for_question(question_id)
        if not judgements:
            return None
        return max(judgements, key=lambda j: j.created_at)
