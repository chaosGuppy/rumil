"""
BaseOrchestrator: abstract base class for all orchestrators.
"""

import asyncio
import logging
import uuid
from abc import ABC, abstractmethod
from collections.abc import Sequence

from rumil.calls.stages import CallRunner
from rumil.constants import SMOKE_TEST_MAX_ROUNDS, compute_round_budget
from rumil.database import DB
from rumil.models import (
    AssessDispatchPayload,
    CallType,
    CreateViewDispatchPayload,
    Dispatch,
)
from rumil.orchestrators.common import (
    _create_broadcaster,
)
from rumil.orchestrators.dispatch_handlers import (
    DISPATCH_HANDLERS,
    DispatchContext,
)
from rumil.settings import get_settings
from rumil.tracing.broadcast import Broadcaster
from rumil.tracing.trace_events import DispatchExecutedEvent
from rumil.tracing.tracer import get_trace
from rumil.views import get_active_view

log = logging.getLogger(__name__)


class BaseOrchestrator(ABC):
    summarise_before_assess: bool = True

    def __init__(self, db: DB, broadcaster: Broadcaster | None = None):
        self.db = db
        self.broadcaster: Broadcaster | None = broadcaster
        self._owns_broadcaster: bool = False
        self.ingest_hint: str = ""
        # Set by prio orchestrators to their root question ID. Sub-call
        # budget consumption debits this question's pool. None for
        # orchestrators outside a per-question prio cycle.
        self.pool_question_id: str | None = None

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
        log.info("Budget pacing: effective=%d, round_allocation=%d", effective, paced)
        return paced

    async def _setup(self) -> None:
        if not self.broadcaster:
            self.broadcaster = _create_broadcaster(self.db)
            self._owns_broadcaster = True
        log.info("Orchestrator: run_id=%s", self.db.run_id)
        total, used = await self.db.get_budget()
        log.info(
            "Orchestrator.run starting: budget=%d (used=%d)",
            total,
            used,
        )

    async def _teardown(self) -> None:
        if self.broadcaster and self._owns_broadcaster:
            await self.broadcaster.close()
        total, used = await self.db.get_budget()
        log.info("Orchestrator.run complete: budget used %d/%d", used, total)

    async def _run_simple_call_dispatch(
        self,
        question_id: str,
        call_type: CallType,
        cls: type[CallRunner],
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
        instance = cls(
            question_id,
            call,
            self.db,
            broadcaster=self.broadcaster,
            max_rounds=max_rounds,
            fruit_threshold=fruit_threshold,
            pool_question_id=self.pool_question_id,
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

        Any dispatch targeting a *non-scope* question triggers a post-sequence
        ``view.refresh(...)`` for that target (deduped), so the subquestion's
        ever-evolving summary stays up-to-date with whatever the dispatch just
        produced. ASSESS and CREATE_VIEW dispatches already update the view
        themselves, so they don't double-fire.

        All dispatches in the sequence are guaranteed to run: if budget
        is exhausted mid-sequence, subsequent dispatches force-consume
        so that trailing calls (e.g. auto-refresh) are never skipped.

        Child call IDs are pre-generated so that DispatchExecutedEvents
        can be recorded before execution begins, making dispatch links
        clickable in the trace frontend immediately.
        """
        pre_ids = [str(uuid.uuid4()) for _ in sequence]
        raw_qids = [d.payload.question_id for d in sequence]
        resolved_map = await self.db.resolve_page_ids(raw_qids)
        resolves = [resolved_map.get(qid) or scope_question_id for qid in raw_qids]
        pages = await self.db.get_pages_by_ids([r for r in resolves if r is not None])
        headlines = [pages[r].headline if r in pages else "" for r in resolves]

        refresh_targets: list[str] = []
        seen_targets: set[str] = set()
        for i, dispatch in enumerate(sequence):
            target = resolves[i]
            if target == scope_question_id or target in seen_targets:
                continue
            if isinstance(dispatch.payload, (AssessDispatchPayload, CreateViewDispatchPayload)):
                continue
            seen_targets.add(target)
            refresh_targets.append(target)

        is_multi_step = (len(sequence) + len(refresh_targets)) > 1
        seq_id: str | None = None
        if is_multi_step:
            call_sequence = await self.db.create_call_sequence(
                parent_call_id=parent_call_id,
                scope_question_id=scope_question_id,
                position_in_batch=position_in_batch,
            )
            seq_id = call_sequence.id

        trace = get_trace()
        if trace:
            for i, dispatch in enumerate(sequence):
                await trace.record(
                    DispatchExecutedEvent(
                        index=base_index + i,
                        child_call_type=dispatch.call_type.value,
                        question_id=resolves[i],
                        question_headline=headlines[i],
                        child_call_id=pre_ids[i],
                    )
                )

        executed = False
        seq_pos = 0
        for i, dispatch in enumerate(sequence):
            force = i > 0 and await self.db.budget_remaining() <= 0
            await self._execute_dispatch(
                dispatch,
                scope_question_id,
                parent_call_id,
                force=force,
                call_id=pre_ids[i],
                sequence_id=seq_id,
                sequence_position=seq_pos if is_multi_step else None,
            )
            if isinstance(dispatch.payload, AssessDispatchPayload):
                seq_pos += 2 if self.summarise_before_assess else 1
            else:
                seq_pos += 1
            executed = True

        if refresh_targets:
            view = get_active_view()
            for target in refresh_targets:
                await view.refresh(
                    target,
                    self.db,
                    parent_call_id=parent_call_id,
                    broadcaster=self.broadcaster,
                    force=True,
                    sequence_id=seq_id,
                    sequence_position=seq_pos if is_multi_step else None,
                    pool_question_id=self.pool_question_id,
                )
                seq_pos += 1
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
                "Dispatch question ID not found: %s, falling back to scope",
                p.question_id[:8],
            )
            resolved = scope_question_id

        d_label = await self.db.page_label(resolved)

        handler = DISPATCH_HANDLERS.get(type(p))
        if handler is None:
            log.warning(
                "No dispatch handler registered for payload type %s",
                type(p).__name__,
            )
            return resolved, None

        ctx = DispatchContext(
            orchestrator=self,
            resolved_question_id=resolved,
            parent_call_id=parent_call_id,
            force=force,
            call_id=call_id,
            sequence_id=sequence_id,
            sequence_position=sequence_position,
            d_label=d_label,
        )
        child_call_id = await handler(ctx, p)
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
            tasks.append(
                self._run_dispatch_sequence(
                    seq,
                    scope_question_id,
                    call_id,
                    base_index,
                    position_in_batch=batch_pos,
                )
            )
            base_index += len(seq)

        sequence_results = await asyncio.gather(*tasks)
        return any(sequence_results)

    @abstractmethod
    async def run(self, root_question_id: str) -> None: ...
