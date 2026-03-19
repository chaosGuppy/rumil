"""
Orchestrator: drives the research workflow using the prioritization system.
Budget is tracked here; prioritization and review calls are free.
"""

import logging
import os

from rumil.tracing.broadcast import Broadcaster
from rumil.calls.summarize import summarize_question
from rumil.calls.call_registry import (
    ASSESS_CALL_CLASSES,
    ASSESS_CONCEPT_CALL_CLASSES,
    INGEST_CALL_CLASSES,
    FIND_CONSIDERATIONS_CALL_CLASSES,
    SCOUT_CONCEPTS_CALL_CLASSES,
    SCOUT_ANALOGIES_CALL_CLASSES,
    SCOUT_ESTIMATES_CALL_CLASSES,
    SCOUT_HYPOTHESES_CALL_CLASSES,
    SCOUT_SUBQUESTIONS_CALL_CLASSES,
    WEB_RESEARCH_CALL_CLASSES,
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
    RecurseDispatchPayload,
    ScoutAnalogiesDispatchPayload,
    ScoutDispatchPayload,
    ScoutEstimatesDispatchPayload,
    ScoutHypothesesDispatchPayload,
    ScoutMode,
    ScoutSubquestionsDispatchPayload,
    WebResearchDispatchPayload,
    Workspace,
)
from rumil.prioritizer import LLMPrioritizer, NewQuestionPrioritizer, Prioritizer
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
        headline=question_text[:120],
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


async def find_considerations_until_done(
    question_id: str,
    db: DB,
    max_rounds: int | None = None,
    fruit_threshold: int = DEFAULT_FRUIT_THRESHOLD,
    parent_call_id: str | None = None,
    context_page_ids: list | None = None,
    mode: ScoutMode = ScoutMode.ALTERNATE,
    broadcaster=None,
) -> tuple[int, list[str]]:
    """Run a cache-aware find-considerations session.

    Creates one Call and delegates to the ScoutCall class, which handles
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
        "find_considerations_until_done: question=%s, max_rounds=%d, fruit_threshold=%d, mode=%s",
        question_id[:8], max_rounds, fruit_threshold, mode.value,
    )

    call = await db.create_call(
        CallType.FIND_CONSIDERATIONS,
        scope_page_id=question_id,
        parent_call_id=parent_call_id,
        context_page_ids=context_page_ids,
    )

    cls = FIND_CONSIDERATIONS_CALL_CLASSES[get_settings().find_considerations_call_variant]
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
        "find_considerations_until_done finished: %d rounds, call=%s",
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

    await summarize_question(question_id, db, parent_call_id=parent_call_id)

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


async def _run_assess_concept_loop(
    concept_id: str,
    db: DB,
    phase: str,
    max_rounds: int,
    fruit_threshold: int,
    parent_call_id: str | None = None,
    broadcaster=None,
) -> dict:
    """Run assess_concept rounds until fruit drops below threshold or max_rounds reached.

    Returns the review dict from the final round.
    """
    from rumil.calls.assess_concept import SCREENING_PHASE, VALIDATION_PHASE
    log.info(
        "_run_assess_concept_loop: concept=%s, phase=%s, max_rounds=%d, threshold=%d",
        concept_id[:8], phase, max_rounds, fruit_threshold,
    )
    last_review: dict = {}
    for i in range(max_rounds):
        call = await db.create_call(
            CallType.ASSESS_CONCEPT,
            scope_page_id=concept_id,
            parent_call_id=parent_call_id,
        )
        cls = ASSESS_CONCEPT_CALL_CLASSES[get_settings().assess_call_variant]
        assess = cls(concept_id, call, db, phase=phase, broadcaster=broadcaster)
        await assess.run()
        last_review = assess.concept_assessment

        remaining_fruit = last_review.get("remaining_fruit", 10)
        log.info(
            "Assess concept round %d/%d (%s): fruit=%d, score=%s",
            i + 1, max_rounds, phase,
            remaining_fruit, last_review.get("score"),
        )
        if remaining_fruit <= fruit_threshold:
            log.info(
                "Concept fruit (%d) at or below threshold (%d), stopping %s phase",
                remaining_fruit, fruit_threshold, phase,
            )
            break

    return last_review


async def run_concept_session(
    question_id: str,
    db: DB,
    broadcaster=None,
) -> None:
    """Run a full concept-generation session for a research question.

    1. Scout concepts — generate proposals for the question's subtree.
    2. For each proposal: run stage-1 screening.
    3. If screening passes: automatically run stage-2 validation.
    """
    from rumil.calls.assess_concept import (
        SCREENING_PHASE,
        SCREENING_FRUIT_THRESHOLD,
        SCREENING_MAX_ROUNDS,
        VALIDATION_PHASE,
        VALIDATION_FRUIT_THRESHOLD,
        VALIDATION_MAX_ROUNDS,
    )
    log.info("run_concept_session: question=%s", question_id[:8])

    scout_call = await db.create_call(
        CallType.SCOUT_CONCEPTS,
        scope_page_id=question_id,
    )
    cls = SCOUT_CONCEPTS_CALL_CLASSES["default"]
    scout = cls(question_id, scout_call, db, broadcaster=broadcaster)
    await scout.run()
    proposed_ids = scout.result.created_page_ids

    log.info(
        "Scout concepts complete: %d proposals for question=%s",
        len(proposed_ids), question_id[:8],
    )

    for concept_id in proposed_ids:
        concept = await db.get_page(concept_id)
        label = concept.headline[:60] if concept else concept_id[:8]
        log.info("Screening concept: %s [%s]", label, concept_id[:8])

        screening_review = await _run_assess_concept_loop(
            concept_id, db,
            phase=SCREENING_PHASE,
            max_rounds=SCREENING_MAX_ROUNDS,
            fruit_threshold=SCREENING_FRUIT_THRESHOLD,
            parent_call_id=scout_call.id,
            broadcaster=broadcaster,
        )

        if not screening_review.get("screening_passed"):
            log.info(
                "Concept [%s] did not pass screening (score=%s)",
                concept_id[:8], screening_review.get("score"),
            )
            continue

        log.info(
            "Concept [%s] passed screening (score=%s), proceeding to validation",
            concept_id[:8], screening_review.get("score"),
        )

        validation_review = await _run_assess_concept_loop(
            concept_id, db,
            phase=VALIDATION_PHASE,
            max_rounds=VALIDATION_MAX_ROUNDS,
            fruit_threshold=VALIDATION_FRUIT_THRESHOLD,
            parent_call_id=scout_call.id,
            broadcaster=broadcaster,
        )

        concept_page = await db.get_page(concept_id)
        if concept_page and concept_page.is_superseded:
            log.info("Concept [%s] was promoted to research workspace", concept_id[:8])
        else:
            log.info(
                "Concept [%s] completed validation but was not promoted (score=%s)",
                concept_id[:8], validation_review.get("score"),
            )


async def web_research_question(
    question_id: str,
    db: DB,
    allowed_domains: list[str] | None = None,
    parent_call_id: str | None = None,
    broadcaster=None,
) -> str | None:
    """Run one web research call on a question. Returns call ID, or None if no budget."""
    log.info('web_research_question: question=%s', question_id[:8])
    if not await _consume_budget(db):
        return None

    call = await db.create_call(
        CallType.WEB_RESEARCH,
        scope_page_id=question_id,
        parent_call_id=parent_call_id,
    )
    cls = WEB_RESEARCH_CALL_CLASSES[get_settings().web_research_call_variant]
    web_research = cls(
        question_id, call, db,
        allowed_domains=allowed_domains,
        broadcaster=broadcaster,
    )
    await web_research.run()
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

    async def _run_simple_call_dispatch(
        self,
        question_id: str,
        call_type: CallType,
        registry: dict,
        parent_call_id: str | None,
    ) -> str | None:
        """Run a simple (single-pass) call dispatch. Consumes 1 budget."""
        if not await _consume_budget(self.db):
            return None

        call = await self.db.create_call(
            call_type,
            scope_page_id=question_id,
            parent_call_id=parent_call_id,
        )
        cls = registry['default']
        instance = cls(question_id, call, self.db, broadcaster=self.broadcaster)
        await instance.run()
        return call.id

    async def _run_recursive_investigation(
        self,
        question_id: str,
        allocated_budget: int,
        parent_call_id: str | None,
    ) -> None:
        """Recursively investigate a child question with a fresh prioritizer."""
        remaining = await self.db.budget_remaining()
        effective_budget = min(allocated_budget, remaining)
        if effective_budget <= 0:
            log.info(
                'Recursive investigation skipped: no budget for question=%s',
                question_id[:8],
            )
            return

        prioritizer = NewQuestionPrioritizer(self.db, broadcaster=self.broadcaster)
        log.info(
            'Recursive investigation: question=%s, budget=%d',
            question_id[:8], effective_budget,
        )

        budget_spent = 0
        while budget_spent < effective_budget:
            remaining = await self.db.budget_remaining()
            if remaining <= 0:
                break

            capped_remaining = min(remaining, effective_budget - budget_spent)
            result = await prioritizer.get_calls(
                question_id, capped_remaining, parent_call_id=parent_call_id,
            )
            if not result.dispatches:
                break

            spent_any = False
            pre_remaining = await self.db.budget_remaining()
            for dispatch in result.dispatches:
                if await self.db.budget_remaining() <= 0:
                    break
                if budget_spent >= effective_budget:
                    break
                await self._execute_dispatch(
                    dispatch, question_id, result.call_id,
                )
                spent_any = True

            post_remaining = await self.db.budget_remaining()
            budget_spent += pre_remaining - post_remaining

            if spent_any:
                prioritizer.mark_executed()
            else:
                break

        log.info('Recursive investigation complete: question=%s', question_id[:8])

    async def _execute_dispatch(
        self,
        dispatch: Dispatch,
        scope_question_id: str,
        parent_call_id: str | None,
    ) -> tuple[str, str | None]:
        """Execute a single dispatch.

        Returns (resolved_question_id, child_call_id).
        """
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
                'Dispatch: find_considerations on %s (mode=%s, fruit_threshold=%d, max_rounds=%d) — %s',
                d_label, p.mode.value, p.fruit_threshold, p.max_rounds, p.reason,
            )
            _, child_ids = await find_considerations_until_done(
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

        elif isinstance(p, ScoutSubquestionsDispatchPayload):
            log.info('Dispatch: scout_subquestions on %s — %s', d_label, p.reason)
            child_call_id = await self._run_simple_call_dispatch(
                resolved, CallType.SCOUT_SUBQUESTIONS,
                SCOUT_SUBQUESTIONS_CALL_CLASSES, parent_call_id,
            )

        elif isinstance(p, ScoutEstimatesDispatchPayload):
            log.info('Dispatch: scout_estimates on %s — %s', d_label, p.reason)
            child_call_id = await self._run_simple_call_dispatch(
                resolved, CallType.SCOUT_ESTIMATES,
                SCOUT_ESTIMATES_CALL_CLASSES, parent_call_id,
            )

        elif isinstance(p, ScoutHypothesesDispatchPayload):
            log.info('Dispatch: scout_hypotheses on %s — %s', d_label, p.reason)
            child_call_id = await self._run_simple_call_dispatch(
                resolved, CallType.SCOUT_HYPOTHESES,
                SCOUT_HYPOTHESES_CALL_CLASSES, parent_call_id,
            )

        elif isinstance(p, ScoutAnalogiesDispatchPayload):
            log.info('Dispatch: scout_analogies on %s — %s', d_label, p.reason)
            child_call_id = await self._run_simple_call_dispatch(
                resolved, CallType.SCOUT_ANALOGIES,
                SCOUT_ANALOGIES_CALL_CLASSES, parent_call_id,
            )

        elif isinstance(p, WebResearchDispatchPayload):
            log.info('Dispatch: web_research on %s — %s', d_label, p.reason)
            child_call_id = await web_research_question(
                resolved, self.db,
                parent_call_id=parent_call_id,
                broadcaster=self.broadcaster,
            )

        elif isinstance(p, RecurseDispatchPayload):
            log.info(
                'Dispatch: recurse on %s (budget=%d) — %s',
                d_label, p.budget, p.reason,
            )
            await self._run_recursive_investigation(
                resolved, p.budget, parent_call_id,
            )

        return resolved, child_call_id

    async def run(self, root_question_id: str) -> None:
        """Entry point: flat loop driven by a pluggable Prioritizer."""
        self.broadcaster = _create_broadcaster(self.db)
        log.info('Orchestrator: run_id=%s', self.db.run_id)

        total, used = await self.db.get_budget()
        log.info(
            'Orchestrator.run starting: root_question=%s, budget=%d',
            root_question_id[:8], total,
        )

        if self._prioritizer:
            prioritizer = self._prioritizer
        elif get_settings().prioritizer_variant == 'new_question':
            prioritizer = NewQuestionPrioritizer(
                self.db, broadcaster=self.broadcaster,
            )
        else:
            prioritizer = LLMPrioritizer(
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

                    resolved, child_call_id = await self._execute_dispatch(
                        dispatch, root_question_id, result.call_id,
                    )
                    spent_any = True

                    if result.trace:
                        await result.trace.record(DispatchExecutedEvent(
                            index=i,
                            child_call_type=dispatch.call_type.value,
                            question_id=resolved,
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
