"""Pluggable prioritization: abstract interface and LLM-based implementation."""

import logging
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass

from rumil.calls import run_prioritization
from rumil.database import DB
from rumil.models import (
    AssessDispatchPayload,
    CallType,
    Dispatch,
    PrioritizationDispatchPayload,
    ScoutDispatchPayload,
    ScoutMode,
    Workspace,
)
from rumil.tracing.broadcast import Broadcaster
from rumil.tracing.tracer import CallTrace

log = logging.getLogger(__name__)

DEFAULT_FRUIT_THRESHOLD = 4
DEFAULT_MAX_ROUNDS = 5


@dataclass
class PrioritizationResult:
    dispatches: Sequence[Dispatch]
    call_id: str | None = None
    trace: CallTrace | None = None


class Prioritizer(ABC):
    @abstractmethod
    async def get_calls(
        self,
        question_id: str,
        budget: int,
        parent_call_id: str | None = None,
    ) -> PrioritizationResult:
        ...


class LLMPrioritizer(Prioritizer):
    """Cursor-based prioritizer that delegates to the LLM prioritization call.

    Maintains an internal plan (list of dispatches) and a cursor. Each
    ``get_calls()`` invocation returns the next batch of executable
    (scout/assess) dispatches. When a sub-prioritization dispatch is
    encountered, it is expanded inline by running a fresh prioritization
    call scoped to that question.
    """

    def __init__(self, db: DB, broadcaster: Broadcaster | None = None):
        self._db = db
        self._broadcaster = broadcaster
        self._plan: list[Dispatch] = []
        self._cursor: int = 0
        self._call_id: str | None = None
        self._trace: CallTrace | None = None
        self._executed_since_last_plan: bool = False
        self._first_call: bool = True

    async def get_calls(
        self,
        question_id: str,
        budget: int,
        parent_call_id: str | None = None,
    ) -> PrioritizationResult:
        if self._cursor >= len(self._plan):
            if not self._first_call and not self._executed_since_last_plan:
                return PrioritizationResult(dispatches=[])

            await self._run_prioritization(question_id, budget, parent_call_id)
            self._first_call = False
            self._executed_since_last_plan = False

            if not self._plan:
                return self._synthesize_default(question_id)

        batch: list[Dispatch] = []
        while self._cursor < len(self._plan):
            dispatch = self._plan[self._cursor]

            if isinstance(dispatch.payload, PrioritizationDispatchPayload):
                if batch:
                    break
                await self._expand_sub_prioritization(
                    dispatch, parent_call_id,
                )
                continue

            batch.append(dispatch)
            self._cursor += 1

        return PrioritizationResult(
            dispatches=batch,
            call_id=self._call_id,
            trace=self._trace,
        )

    def mark_executed(self) -> None:
        """Signal that at least one dispatch from the last batch was executed."""
        self._executed_since_last_plan = True

    async def _run_prioritization(
        self,
        question_id: str,
        budget: int,
        parent_call_id: str | None,
    ) -> None:
        p_call = await self._db.create_call(
            CallType.PRIORITIZATION,
            scope_page_id=question_id,
            parent_call_id=parent_call_id,
            budget_allocated=budget,
            workspace=Workspace.PRIORITIZATION,
        )

        plan = await run_prioritization(
            scope_question_id=question_id,
            call=p_call,
            budget=budget,
            db=self._db,
            broadcaster=self._broadcaster,
        )

        self._plan = list(plan.get('dispatches', []))
        self._cursor = 0
        self._call_id = p_call.id
        self._trace = plan.get('trace')

        log.debug(
            'LLMPrioritizer: got %d dispatches for question=%s',
            len(self._plan), question_id[:8],
        )

    async def _expand_sub_prioritization(
        self,
        dispatch: Dispatch,
        parent_call_id: str | None,
    ) -> None:
        """Replace a PrioritizationDispatch at the cursor with its expansion."""
        payload = dispatch.payload
        assert isinstance(payload, PrioritizationDispatchPayload)

        resolved = await self._db.resolve_page_id(payload.question_id)
        if not resolved:
            log.warning(
                'Sub-prioritization question ID not found: %s',
                payload.question_id[:8],
            )
            self._cursor += 1
            return

        d_label = await self._db.page_label(resolved)
        log.info(
            'Expanding sub-prioritization on %s (budget=%d) — %s',
            d_label, payload.budget, payload.reason,
        )

        p_call = await self._db.create_call(
            CallType.PRIORITIZATION,
            scope_page_id=resolved,
            parent_call_id=self._call_id or parent_call_id,
            budget_allocated=payload.budget,
            workspace=Workspace.PRIORITIZATION,
        )

        plan = await run_prioritization(
            scope_question_id=resolved,
            call=p_call,
            budget=payload.budget,
            db=self._db,
            broadcaster=self._broadcaster,
        )

        sub_dispatches = list(plan.get('dispatches', []))
        self._plan[self._cursor:self._cursor + 1] = sub_dispatches
        self._call_id = p_call.id
        self._trace = plan.get('trace')

        log.debug(
            'Sub-prioritization expanded to %d dispatches',
            len(sub_dispatches),
        )

    def _synthesize_default(self, question_id: str) -> PrioritizationResult:
        """Return default scout+assess when the LLM produces no dispatches."""
        log.info(
            'No dispatches from prioritization, synthesizing default '
            'scout+assess for question=%s', question_id[:8],
        )
        return PrioritizationResult(
            dispatches=[
                Dispatch(
                    call_type=CallType.SCOUT,
                    payload=ScoutDispatchPayload(
                        question_id=question_id,
                        mode=ScoutMode.ALTERNATE,
                        fruit_threshold=DEFAULT_FRUIT_THRESHOLD,
                        max_rounds=DEFAULT_MAX_ROUNDS,
                        reason="fallback"
                    ),
                ),
                Dispatch(
                    call_type=CallType.ASSESS,
                    payload=AssessDispatchPayload(
                        question_id=question_id,
                        reason="fallback"
                    ),
                ),
            ],
            call_id=self._call_id,
            trace=self._trace,
        )
