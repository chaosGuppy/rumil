"""
Orchestrator: drives the research workflow using the prioritization system.
Budget is tracked here; prioritization and review calls are free.
"""

import logging
import os

from rumil.tracing.broadcast import Broadcaster
from rumil.calls.call_registry import (
    ASSESS_CALL_CLASSES,
    INGEST_CALL_CLASSES,
    SCOUT_CALL_CLASSES,
)
from rumil.database import DB
from rumil.settings import get_settings
from rumil.models import (
    AssessDispatchPayload,
    CallType,
    Dispatch,
    Page,
    PageLayer,
    PageType,
    ScoutDispatchPayload,
    ScoutMode,
    Workspace,
)
from rumil.prioritizer import LLMPrioritizer, Prioritizer
from rumil.tracing.trace_events import DispatchExecutedEvent


log = logging.getLogger(__name__)

DEFAULT_FRUIT_THRESHOLD = 4
DEFAULT_MAX_ROUNDS = 5
DEFAULT_INGEST_FRUIT_THRESHOLD = 5
DEFAULT_INGEST_MAX_ROUNDS = 5

SMOKE_TEST_MAX_ROUNDS = 1
SMOKE_TEST_INGEST_MAX_ROUNDS = 1


async def create_root_question(question_text: str, db: DB) -> str:
    page = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=question_text,
        summary=question_text[:120],
        epistemic_status=2.5,
        epistemic_type="open question",
        provenance_model="human",
        provenance_call_type="init",
        provenance_call_id="init",
        extra={"status": "open"},
    )
    await db.save_page(page)
    return page.id


async def _consume_budget(db: DB) -> bool:
    """Consume one unit of global budget. Returns False if exhausted."""
    ok = await db.consume_budget(1)
    if not ok:
        remaining = await db.budget_remaining()
        log.info("Budget exhausted (remaining: %d)", remaining)
    return ok


async def scout_until_done(
    question_id: str,
    db: DB,
    max_rounds: int | None = None,
    fruit_threshold: int = DEFAULT_FRUIT_THRESHOLD,
    parent_call_id: str | None = None,
    context_page_ids: list | None = None,
    mode: ScoutMode = ScoutMode.ALTERNATE,
    broadcaster=None,
) -> tuple[int, list[str]]:
    """Run a cache-aware scout session.

    Creates one Call and delegates to run_scout_session, which handles
    multi-round looping with conversation resumption, lightweight fruit
    checks, and a single closing review at the end.

    Returns (rounds_made, list_of_call_ids).
    """
    if max_rounds is None:
        max_rounds = (
            SMOKE_TEST_MAX_ROUNDS if get_settings().is_smoke_test
            else DEFAULT_MAX_ROUNDS
        )
    log.info(
        "scout_until_done: question=%s, max_rounds=%d, fruit_threshold=%d, mode=%s",
        question_id[:8], max_rounds, fruit_threshold, mode.value,
    )

    call = await db.create_call(
        CallType.SCOUT,
        scope_page_id=question_id,
        parent_call_id=parent_call_id,
        context_page_ids=context_page_ids,
    )

    cls = SCOUT_CALL_CLASSES[get_settings().scout_call_variant]
    scout = cls(
        question_id, call, db,
        max_rounds=max_rounds,
        fruit_threshold=fruit_threshold,
        mode=mode,
        context_page_ids=context_page_ids,
        broadcaster=broadcaster,
    )
    await scout.run()

    log.info(
        "scout_until_done finished: %d rounds, call=%s",
        scout.rounds_completed, call.id[:8],
    )
    return scout.rounds_completed, [call.id]


async def ingest_until_done(
    source_page: Page,
    question_id: str,
    db: DB,
    max_rounds: int | None = None,
    fruit_threshold: int = DEFAULT_INGEST_FRUIT_THRESHOLD,
    parent_call_id: str | None = None,
    broadcaster=None,
) -> int:
    """
    Run Ingest rounds on a source/question pair until remaining_fruit falls below
    fruit_threshold or max_rounds is reached. Returns number of Ingest calls made.
    fruit_threshold is the primary stopping condition; max_rounds is a failsafe.
    Each round sees previously extracted claims via the question's working context.
    """
    if max_rounds is None:
        max_rounds = (
            SMOKE_TEST_INGEST_MAX_ROUNDS if get_settings().is_smoke_test
            else DEFAULT_INGEST_MAX_ROUNDS
        )
    log.info(
        "ingest_until_done: source=%s, question=%s, max_rounds=%d",
        source_page.id[:8], question_id[:8], max_rounds,
    )
    rounds = 0
    for i in range(max_rounds):
        if not await _consume_budget(db):
            break

        call = await db.create_call(
            CallType.INGEST,
            scope_page_id=source_page.id,
            parent_call_id=parent_call_id,
        )
        cls = INGEST_CALL_CLASSES[get_settings().ingest_call_variant]
        ingest = cls(source_page, question_id, call, db, broadcaster=broadcaster)
        await ingest.run()
        review = ingest.review
        rounds += 1

        remaining_fruit = review.get("remaining_fruit", 5) if review else 5
        log.info(
            "Ingest round %d/%d: remaining_fruit=%d (threshold=%d)",
            i + 1, max_rounds, remaining_fruit, fruit_threshold,
        )

        if remaining_fruit <= fruit_threshold:
            log.info(
                "Ingest fruit (%d) below threshold (%d), stopping",
                remaining_fruit, fruit_threshold,
            )
            break

    log.info("ingest_until_done finished: %d rounds", rounds)
    return rounds


async def assess_question(
    question_id: str,
    db: DB,
    parent_call_id: str | None = None,
    context_page_ids: list | None = None,
    broadcaster=None,
) -> str | None:
    """Run one Assess call on a question. Returns call ID, or None if no budget."""
    log.info("assess_question: question=%s", question_id[:8])
    if not await _consume_budget(db):
        return None

    call = await db.create_call(
        CallType.ASSESS,
        scope_page_id=question_id,
        parent_call_id=parent_call_id,
        context_page_ids=context_page_ids,
    )
    cls = ASSESS_CALL_CLASSES[get_settings().assess_call_variant]
    assess = cls(question_id, call, db, broadcaster=broadcaster)
    await assess.run()
    return call.id


def _create_broadcaster(db: DB) -> Broadcaster | None:
    """Create a broadcaster for the given DB's run_id, or None if disabled."""
    if os.environ.get("RUMIL_TEST_MODE"):
        return None
    settings = get_settings()
    url, key = settings.get_supabase_credentials(prod=settings.is_prod_db)
    return Broadcaster(db.run_id, url, key)


class Orchestrator:
    def __init__(self, db: DB, prioritizer: Prioritizer | None = None):
        self.db = db
        self.broadcaster: Broadcaster | None = None
        self._prioritizer = prioritizer

    async def _execute_dispatch(
        self,
        dispatch: Dispatch,
        scope_question_id: str,
        parent_call_id: str | None,
    ) -> str | None:
        """Execute a single scout or assess dispatch. Returns child call ID."""
        p = dispatch.payload

        resolved = await self.db.resolve_page_id(p.question_id)
        if not resolved:
            log.warning(
                'Dispatch question ID not found: %s, falling back to scope',
                p.question_id[:8],
            )
            resolved = scope_question_id

        d_label = await self.db.page_label(resolved)
        child_call_id: str | None = None

        if isinstance(p, ScoutDispatchPayload):
            log.info(
                'Dispatch: scout on %s (mode=%s, fruit_threshold=%d, max_rounds=%d) — %s',
                d_label, p.mode.value, p.fruit_threshold, p.max_rounds, p.reason,
            )
            _, child_ids = await scout_until_done(
                resolved,
                self.db,
                max_rounds=p.max_rounds,
                fruit_threshold=p.fruit_threshold,
                parent_call_id=parent_call_id,
                context_page_ids=p.context_page_ids,
                mode=p.mode,
                broadcaster=self.broadcaster,
            )
            child_call_id = child_ids[0] if child_ids else None

        elif isinstance(p, AssessDispatchPayload):
            log.info('Dispatch: assess on %s — %s', d_label, p.reason)
            child_call_id = await assess_question(
                resolved,
                self.db,
                parent_call_id=parent_call_id,
                context_page_ids=p.context_page_ids,
                broadcaster=self.broadcaster,
            )

        return child_call_id

    async def run(self, root_question_id: str) -> None:
        """Entry point: flat loop driven by a pluggable Prioritizer."""
        self.broadcaster = _create_broadcaster(self.db)
        log.info('Orchestrator: run_id=%s', self.db.run_id)

        total, used = await self.db.get_budget()
        log.info(
            'Orchestrator.run starting: root_question=%s, budget=%d',
            root_question_id[:8], total,
        )

        prioritizer = self._prioritizer or LLMPrioritizer(
            self.db, broadcaster=self.broadcaster,
        )

        try:
            while True:
                remaining = await self.db.budget_remaining()
                if remaining <= 0:
                    break

                result = await prioritizer.get_calls(
                    root_question_id, remaining,
                )
                if not result.dispatches:
                    break

                spent_any = False
                for i, dispatch in enumerate(result.dispatches):
                    if await self.db.budget_remaining() <= 0:
                        break

                    child_call_id = await self._execute_dispatch(
                        dispatch, root_question_id, result.call_id,
                    )
                    spent_any = True

                    if result.trace:
                        await result.trace.record(DispatchExecutedEvent(
                            index=i,
                            child_call_type=dispatch.call_type.value,
                            question_id=dispatch.payload.question_id,
                            child_call_id=child_call_id,
                        ))

                if spent_any:
                    prioritizer.mark_executed()
                else:
                    break
        finally:
            if self.broadcaster:
                await self.broadcaster.close()

        total, used = await self.db.get_budget()
        log.info('Orchestrator.run complete: budget used %d/%d', used, total)
