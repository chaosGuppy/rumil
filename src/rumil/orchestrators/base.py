"""
BaseOrchestrator: abstract base class for all orchestrators.
"""

import asyncio
import logging
import uuid
from abc import ABC, abstractmethod
from collections.abc import Sequence

from rumil.available_calls import get_available_calls_preset
from rumil.constants import compute_round_budget
from rumil.calls.call_registry import (
    SCOUT_ANALOGIES_CALL_CLASSES,
    SCOUT_C_CRUXES_CALL_CLASSES,
    SCOUT_C_HOW_FALSE_CALL_CLASSES,
    SCOUT_C_HOW_TRUE_CALL_CLASSES,
    SCOUT_C_RELEVANT_EVIDENCE_CALL_CLASSES,
    SCOUT_C_STRESS_TEST_CASES_CALL_CLASSES,
    SCOUT_DEEP_QUESTIONS_CALL_CLASSES,
    SCOUT_ESTIMATES_CALL_CLASSES,
    SCOUT_FACTCHECKS_CALL_CLASSES,
    SCOUT_HYPOTHESES_CALL_CLASSES,
    SCOUT_PARADIGM_CASES_CALL_CLASSES,
    SCOUT_SUBQUESTIONS_CALL_CLASSES,
    SCOUT_WEB_QUESTIONS_CALL_CLASSES,
)
from rumil.constants import SMOKE_TEST_MAX_ROUNDS
from rumil.database import DB
from rumil.models import (
    AssessDispatchPayload,
    CallType,
    Dispatch,
    ScoutAnalogiesDispatchPayload,
    ScoutCCruxesDispatchPayload,
    ScoutCHowFalseDispatchPayload,
    ScoutCHowTrueDispatchPayload,
    ScoutCRelevantEvidenceDispatchPayload,
    ScoutCStressTestCasesDispatchPayload,
    ScoutDeepQuestionsDispatchPayload,
    ScoutDispatchPayload,
    ScoutEstimatesDispatchPayload,
    ScoutFactchecksDispatchPayload,
    ScoutHypothesesDispatchPayload,
    ScoutParadigmCasesDispatchPayload,
    ScoutSubquestionsDispatchPayload,
    ScoutWebQuestionsDispatchPayload,
    WebResearchDispatchPayload,
)
from rumil.orchestrators.common import (
    _consume_budget,
    _create_broadcaster,
    assess_question,
    find_considerations_until_done,
    web_research_question,
)
from rumil.settings import get_settings
from rumil.tracing.broadcast import Broadcaster
from rumil.tracing.trace_events import DispatchExecutedEvent
from rumil.tracing.tracer import get_trace


log = logging.getLogger(__name__)


class BaseOrchestrator(ABC):
    def __init__(self, db: DB, broadcaster: Broadcaster | None = None):
        self.db = db
        self.broadcaster: Broadcaster | None = broadcaster
        self._owns_broadcaster: bool = False

    async def _pacing_params(self) -> tuple[int, int]:
        """Return (total, used) for budget pacing.

        Subclasses with budget_cap should override to use their local scope.
        """
        return await self.db.get_budget()

    async def _paced_budget(self, effective: int) -> int:
        """Apply budget pacing to an effective budget, if enabled."""
        if not get_settings().budget_pacing_enabled:
            return effective
        total, used = await self._pacing_params()
        paced = min(effective, compute_round_budget(total, used))
        log.info('Budget pacing: effective=%d, round_allocation=%d', effective, paced)
        return paced

    async def _setup(self) -> None:
        if not self.broadcaster:
            self.broadcaster = _create_broadcaster(self.db)
            self._owns_broadcaster = True
        log.info('Orchestrator: run_id=%s', self.db.run_id)
        total, used = await self.db.get_budget()
        log.info(
            'Orchestrator.run starting: budget=%d (used=%d)',
            total, used,
        )

    async def _teardown(self) -> None:
        if self.broadcaster and self._owns_broadcaster:
            await self.broadcaster.close()
        total, used = await self.db.get_budget()
        log.info('Orchestrator.run complete: budget used %d/%d', used, total)

    async def _run_simple_call_dispatch(
        self,
        question_id: str,
        call_type: CallType,
        registry: dict,
        parent_call_id: str | None,
        force: bool = False,
        call_id: str | None = None,
        sequence_id: str | None = None,
        sequence_position: int | None = None,
        max_rounds: int = 5,
        fruit_threshold: int = 4,
    ) -> str | None:
        """Run a call dispatch with optional multi-round support.

        Budget consumption is handled internally by MultiRoundLoop
        (one unit per round), matching how find_considerations works.
        """
        if get_settings().is_smoke_test:
            max_rounds = min(max_rounds, SMOKE_TEST_MAX_ROUNDS)

        if force and await self.db.budget_remaining() <= 0:
            await self.db.add_budget(1)

        call = await self.db.create_call(
            call_type,
            scope_page_id=question_id,
            parent_call_id=parent_call_id,
            call_id=call_id,
            sequence_id=sequence_id,
            sequence_position=sequence_position,
        )
        cls = registry['default']
        instance = cls(
            question_id, call, self.db,
            broadcaster=self.broadcaster,
            max_rounds=max_rounds,
            fruit_threshold=fruit_threshold,
        )
        await instance.run()
        return call.id

    async def _run_dispatch_sequence(
        self,
        sequence: Sequence[Dispatch],
        scope_question_id: str,
        parent_call_id: str | None,
        base_index: int,
        position_in_batch: int = 0,
    ) -> bool:
        """Run dispatches in a sequence sequentially. Returns True if any executed.

        All dispatches in the sequence are guaranteed to run: if budget
        is exhausted mid-sequence, subsequent dispatches force-consume
        so that trailing calls (e.g. auto-assess) are never skipped.

        Child call IDs are pre-generated so that DispatchExecutedEvents
        can be recorded before execution begins, making dispatch links
        clickable in the trace frontend immediately.
        """
        is_multi_step = len(sequence) > 1
        seq_id: str | None = None
        if is_multi_step:
            call_sequence = await self.db.create_call_sequence(
                parent_call_id=parent_call_id,
                scope_question_id=scope_question_id,
                position_in_batch=position_in_batch,
            )
            seq_id = call_sequence.id

        pre_ids = [str(uuid.uuid4()) for _ in sequence]
        resolves = []
        headlines = []
        for dispatch in sequence:
            resolved = await self.db.resolve_page_id(dispatch.payload.question_id)
            resolved = resolved or scope_question_id
            resolves.append(resolved)
            page = await self.db.get_page(resolved)
            headlines.append(page.headline if page else '')

        trace = get_trace()
        if trace:
            for i, dispatch in enumerate(sequence):
                await trace.record(DispatchExecutedEvent(
                    index=base_index + i,
                    child_call_type=dispatch.call_type.value,
                    question_id=resolves[i],
                    question_headline=headlines[i],
                    child_call_id=pre_ids[i],
                ))

        executed = False
        seq_pos = 0
        for i, dispatch in enumerate(sequence):
            force = i > 0 and await self.db.budget_remaining() <= 0
            await self._execute_dispatch(
                dispatch, scope_question_id, parent_call_id,
                force=force, call_id=pre_ids[i],
                sequence_id=seq_id,
                sequence_position=seq_pos if is_multi_step else None,
            )
            if isinstance(dispatch.payload, AssessDispatchPayload):
                seq_pos += 2
            else:
                seq_pos += 1
            executed = True
        return executed

    async def _execute_dispatch(
        self,
        dispatch: Dispatch,
        scope_question_id: str,
        parent_call_id: str | None,
        *,
        force: bool = False,
        call_id: str | None = None,
        sequence_id: str | None = None,
        sequence_position: int | None = None,
    ) -> tuple[str, str | None]:
        """Execute a single dispatch.

        When *force* is True, budget is expanded if needed so the call
        always proceeds (used for trailing dispatches in a committed batch).

        When *call_id* is provided, the child call will be created with
        that ID (for eager link creation in traces).

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
                force=force,
                call_id=call_id,
                sequence_id=sequence_id,
                sequence_position=sequence_position,
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
                force=force,
                call_id=call_id,
                sequence_id=sequence_id,
                sequence_position=sequence_position,
            )

        elif isinstance(p, ScoutSubquestionsDispatchPayload):
            log.info('Dispatch: scout_subquestions on %s (max_rounds=%d) — %s', d_label, p.max_rounds, p.reason)
            child_call_id = await self._run_simple_call_dispatch(
                resolved, CallType.SCOUT_SUBQUESTIONS,
                SCOUT_SUBQUESTIONS_CALL_CLASSES, parent_call_id,
                force=force, call_id=call_id,
                sequence_id=sequence_id, sequence_position=sequence_position,
                max_rounds=p.max_rounds, fruit_threshold=p.fruit_threshold,
            )

        elif isinstance(p, ScoutEstimatesDispatchPayload):
            log.info('Dispatch: scout_estimates on %s (max_rounds=%d) — %s', d_label, p.max_rounds, p.reason)
            child_call_id = await self._run_simple_call_dispatch(
                resolved, CallType.SCOUT_ESTIMATES,
                SCOUT_ESTIMATES_CALL_CLASSES, parent_call_id,
                force=force, call_id=call_id,
                sequence_id=sequence_id, sequence_position=sequence_position,
                max_rounds=p.max_rounds, fruit_threshold=p.fruit_threshold,
            )

        elif isinstance(p, ScoutHypothesesDispatchPayload):
            log.info('Dispatch: scout_hypotheses on %s (max_rounds=%d) — %s', d_label, p.max_rounds, p.reason)
            child_call_id = await self._run_simple_call_dispatch(
                resolved, CallType.SCOUT_HYPOTHESES,
                SCOUT_HYPOTHESES_CALL_CLASSES, parent_call_id,
                force=force, call_id=call_id,
                sequence_id=sequence_id, sequence_position=sequence_position,
                max_rounds=p.max_rounds, fruit_threshold=p.fruit_threshold,
            )

        elif isinstance(p, ScoutAnalogiesDispatchPayload):
            log.info('Dispatch: scout_analogies on %s (max_rounds=%d) — %s', d_label, p.max_rounds, p.reason)
            child_call_id = await self._run_simple_call_dispatch(
                resolved, CallType.SCOUT_ANALOGIES,
                SCOUT_ANALOGIES_CALL_CLASSES, parent_call_id,
                force=force, call_id=call_id,
                sequence_id=sequence_id, sequence_position=sequence_position,
                max_rounds=p.max_rounds, fruit_threshold=p.fruit_threshold,
            )

        elif isinstance(p, ScoutParadigmCasesDispatchPayload):
            log.info('Dispatch: scout_paradigm_cases on %s (max_rounds=%d) — %s', d_label, p.max_rounds, p.reason)
            child_call_id = await self._run_simple_call_dispatch(
                resolved, CallType.SCOUT_PARADIGM_CASES,
                SCOUT_PARADIGM_CASES_CALL_CLASSES, parent_call_id,
                force=force, call_id=call_id,
                sequence_id=sequence_id, sequence_position=sequence_position,
                max_rounds=p.max_rounds, fruit_threshold=p.fruit_threshold,
            )

        elif isinstance(p, ScoutFactchecksDispatchPayload):
            log.info('Dispatch: scout_factchecks on %s (max_rounds=%d) — %s', d_label, p.max_rounds, p.reason)
            child_call_id = await self._run_simple_call_dispatch(
                resolved, CallType.SCOUT_FACTCHECKS,
                SCOUT_FACTCHECKS_CALL_CLASSES, parent_call_id,
                force=force, call_id=call_id,
                sequence_id=sequence_id, sequence_position=sequence_position,
                max_rounds=p.max_rounds, fruit_threshold=p.fruit_threshold,
            )

        elif isinstance(p, ScoutWebQuestionsDispatchPayload):
            log.info('Dispatch: scout_web_questions on %s (max_rounds=%d) — %s', d_label, p.max_rounds, p.reason)
            child_call_id = await self._run_simple_call_dispatch(
                resolved, CallType.SCOUT_WEB_QUESTIONS,
                SCOUT_WEB_QUESTIONS_CALL_CLASSES, parent_call_id,
                force=force, call_id=call_id,
                sequence_id=sequence_id, sequence_position=sequence_position,
                max_rounds=p.max_rounds, fruit_threshold=p.fruit_threshold,
            )

        elif isinstance(p, ScoutDeepQuestionsDispatchPayload):
            log.info('Dispatch: scout_deep_questions on %s (max_rounds=%d) — %s', d_label, p.max_rounds, p.reason)
            child_call_id = await self._run_simple_call_dispatch(
                resolved, CallType.SCOUT_DEEP_QUESTIONS,
                SCOUT_DEEP_QUESTIONS_CALL_CLASSES, parent_call_id,
                force=force, call_id=call_id,
                sequence_id=sequence_id, sequence_position=sequence_position,
                max_rounds=p.max_rounds, fruit_threshold=p.fruit_threshold,
            )

        elif isinstance(p, ScoutCHowTrueDispatchPayload):
            log.info('Dispatch: scout_c_how_true on %s (max_rounds=%d) — %s', d_label, p.max_rounds, p.reason)
            child_call_id = await self._run_simple_call_dispatch(
                resolved, CallType.SCOUT_C_HOW_TRUE,
                SCOUT_C_HOW_TRUE_CALL_CLASSES, parent_call_id,
                force=force, call_id=call_id,
                sequence_id=sequence_id, sequence_position=sequence_position,
                max_rounds=p.max_rounds, fruit_threshold=p.fruit_threshold,
            )

        elif isinstance(p, ScoutCHowFalseDispatchPayload):
            log.info('Dispatch: scout_c_how_false on %s (max_rounds=%d) — %s', d_label, p.max_rounds, p.reason)
            child_call_id = await self._run_simple_call_dispatch(
                resolved, CallType.SCOUT_C_HOW_FALSE,
                SCOUT_C_HOW_FALSE_CALL_CLASSES, parent_call_id,
                force=force, call_id=call_id,
                sequence_id=sequence_id, sequence_position=sequence_position,
                max_rounds=p.max_rounds, fruit_threshold=p.fruit_threshold,
            )

        elif isinstance(p, ScoutCCruxesDispatchPayload):
            log.info('Dispatch: scout_c_cruxes on %s (max_rounds=%d) — %s', d_label, p.max_rounds, p.reason)
            child_call_id = await self._run_simple_call_dispatch(
                resolved, CallType.SCOUT_C_CRUXES,
                SCOUT_C_CRUXES_CALL_CLASSES, parent_call_id,
                force=force, call_id=call_id,
                sequence_id=sequence_id, sequence_position=sequence_position,
                max_rounds=p.max_rounds, fruit_threshold=p.fruit_threshold,
            )

        elif isinstance(p, ScoutCRelevantEvidenceDispatchPayload):
            log.info('Dispatch: scout_c_relevant_evidence on %s (max_rounds=%d) — %s', d_label, p.max_rounds, p.reason)
            child_call_id = await self._run_simple_call_dispatch(
                resolved, CallType.SCOUT_C_RELEVANT_EVIDENCE,
                SCOUT_C_RELEVANT_EVIDENCE_CALL_CLASSES, parent_call_id,
                force=force, call_id=call_id,
                sequence_id=sequence_id, sequence_position=sequence_position,
                max_rounds=p.max_rounds, fruit_threshold=p.fruit_threshold,
            )

        elif isinstance(p, ScoutCStressTestCasesDispatchPayload):
            log.info('Dispatch: scout_c_stress_test_cases on %s (max_rounds=%d) — %s', d_label, p.max_rounds, p.reason)
            child_call_id = await self._run_simple_call_dispatch(
                resolved, CallType.SCOUT_C_STRESS_TEST_CASES,
                SCOUT_C_STRESS_TEST_CASES_CALL_CLASSES, parent_call_id,
                force=force, call_id=call_id,
                sequence_id=sequence_id, sequence_position=sequence_position,
                max_rounds=p.max_rounds, fruit_threshold=p.fruit_threshold,
            )

        elif isinstance(p, WebResearchDispatchPayload):
            log.info('Dispatch: web_research on %s — %s', d_label, p.reason)
            child_call_id = await web_research_question(
                resolved, self.db,
                parent_call_id=parent_call_id,
                broadcaster=self.broadcaster,
                force=force,
                call_id=call_id,
                sequence_id=sequence_id,
                sequence_position=sequence_position,
            )

        return resolved, child_call_id

    async def _run_sequences(
        self,
        sequences: Sequence[Sequence[Dispatch]],
        scope_question_id: str,
        call_id: str | None,
    ) -> bool:
        """Run multiple dispatch sequences concurrently. Returns True if any executed."""
        base_index = 0
        tasks = []
        for batch_pos, seq in enumerate(sequences):
            tasks.append(self._run_dispatch_sequence(
                seq, scope_question_id, call_id,
                base_index,
                position_in_batch=batch_pos,
            ))
            base_index += len(seq)

        sequence_results = await asyncio.gather(*tasks)
        return any(sequence_results)

    @abstractmethod
    async def run(self, root_question_id: str) -> None: ...
