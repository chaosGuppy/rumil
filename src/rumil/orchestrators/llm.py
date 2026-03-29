"""
LLMOrchestrator: cursor-based orchestrator that delegates planning to the LLM prioritization call.
"""

import logging

from rumil.calls import run_prioritization
from rumil.calls.common import mark_call_completed
from rumil.constants import DEFAULT_FRUIT_THRESHOLD, DEFAULT_MAX_ROUNDS
from rumil.database import DB
from rumil.models import (
    AssessDispatchPayload,
    CallType,
    Dispatch,
    PrioritizationDispatchPayload,
    ScoutDispatchPayload,
    Workspace,
)
from rumil.orchestrators.base import BaseOrchestrator
from rumil.orchestrators.common import PrioritizationResult
from rumil.settings import get_settings
from rumil.tracing.broadcast import Broadcaster


log = logging.getLogger(__name__)


class LLMOrchestrator(BaseOrchestrator):
    """Cursor-based orchestrator that delegates planning to the LLM prioritization call.

    Maintains an internal plan (list of dispatches) and a cursor. Each
    loop iteration returns the next batch of executable dispatches. When
    a sub-prioritization dispatch is encountered, it is expanded inline
    by running a fresh prioritization call scoped to that question.
    """

    def __init__(self, db: DB, broadcaster: Broadcaster | None = None):
        super().__init__(db, broadcaster)
        self._plan: list[Dispatch] = []
        self._cursor: int = 0
        self._call_id: str | None = None

        self._executed_since_last_plan: bool = False
        self._first_call: bool = True

    async def run(self, root_question_id: str) -> None:
        await self._setup()
        try:
            while True:
                remaining = await self.db.budget_remaining()
                if remaining <= 0:
                    break

                result = await self._get_next_batch(root_question_id, remaining)
                if not result.dispatch_sequences:
                    break

                executed = await self._run_sequences(
                    result.dispatch_sequences, root_question_id,
                    result.call_id,
                )
                if executed:
                    self._executed_since_last_plan = True
                else:
                    break
        finally:
            await self._teardown()

    async def _get_next_batch(
        self,
        question_id: str,
        budget: int,
        parent_call_id: str | None = None,
    ) -> PrioritizationResult:
        if self._cursor >= len(self._plan):
            if not self._first_call and not self._executed_since_last_plan:
                return PrioritizationResult(dispatch_sequences=[])

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
            dispatch_sequences=[batch] if batch else [],
            call_id=self._call_id,
        )

    async def _run_prioritization(
        self,
        question_id: str,
        budget: int,
        parent_call_id: str | None,
    ) -> None:
        p_call = await self.db.create_call(
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
            db=self.db,
            broadcaster=self.broadcaster,
        )

        self._plan = list(plan.get('dispatches', []))
        self._cursor = 0
        self._call_id = p_call.id

        log.debug(
            'LLMOrchestrator: got %d dispatches for question=%s',
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

        resolved = await self.db.resolve_page_id(payload.question_id)
        if not resolved:
            log.warning(
                'Sub-prioritization question ID not found: %s',
                payload.question_id[:8],
            )
            self._cursor += 1
            return

        d_label = await self.db.page_label(resolved)
        log.info(
            'Expanding sub-prioritization on %s (budget=%d) — %s',
            d_label, payload.budget, payload.reason,
        )

        p_call = await self.db.create_call(
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
            db=self.db,
            broadcaster=self.broadcaster,
        )

        sub_dispatches = list(plan.get('dispatches', []))
        self._plan[self._cursor:self._cursor + 1] = sub_dispatches
        self._call_id = p_call.id

        log.debug(
            'Sub-prioritization expanded to %d dispatches',
            len(sub_dispatches),
        )

    def _synthesize_default(self, question_id: str) -> PrioritizationResult:
        """Return default find_considerations+assess when the LLM produces no dispatches."""
        log.info(
            'No dispatches from prioritization, synthesizing default '
            'find_considerations+assess for question=%s', question_id[:8],
        )
        return PrioritizationResult(
            dispatch_sequences=[[
                Dispatch(
                    call_type=CallType.FIND_CONSIDERATIONS,
                    payload=ScoutDispatchPayload(
                        question_id=question_id,
                        mode=get_settings().allowed_find_considerations_modes[0],
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
            ]],
            call_id=self._call_id,
        )
