"""Assess call: synthesise considerations and render a judgement."""

import logging

from rumil.calls.base import SimpleCall
from rumil.calls.common import RunCallResult
from rumil.context import build_call_context
from rumil.database import DB
from rumil.models import Call, CallType

log = logging.getLogger(__name__)


class AssessCall(SimpleCall):
    """Assess a question: weigh considerations and produce a judgement."""

    def call_type(self) -> CallType:
        return CallType.ASSESS

    def task_description(self) -> str:
        return (
            "Assess this question and render a judgement.\n\n"
            f"Question ID: `{self.question_id}`\n\n"
            "Synthesise the considerations, weigh evidence on multiple sides, "
            "and produce a judgement with structured confidence. "
            "Even if uncertain, commit to a position."
        )

    def result_summary(self) -> str:
        return f"Assess complete. Created {len(self.result.created_page_ids)} pages."

    async def build_context(self) -> None:
        self.context_text, _, self.working_page_ids = await build_call_context(
            self.question_id, self.db, extra_page_ids=self.preloaded_ids,
        )
        await self._record_context_built()
        await self._load_phase1_pages()

    def _log_review(self, review: dict) -> None:
        log.info(
            "Assess review: confidence=%s, self_assessment=%s",
            review.get("confidence_in_output", "?"),
            review.get("self_assessment", "")[:80],
        )


async def run_assess(
    question_id: str,
    call: Call,
    db: DB,
    broadcaster=None,
) -> tuple[RunCallResult, dict]:
    """Run an Assess call on a question.

    Returns (run_call_result, review_dict).
    """
    log.info("Assess starting: call=%s, question=%s", call.id[:8], question_id[:8])
    assess = AssessCall(question_id, call, db, broadcaster=broadcaster)
    await assess.run()
    log.info(
        "Assess complete: call=%s, pages_created=%d",
        call.id[:8], len(assess.result.created_page_ids),
    )
    return assess.result, assess.review
