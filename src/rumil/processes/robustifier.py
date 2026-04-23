"""Robustifier process: wraps RobustifyOrchestrator to produce a VariantSetDelta.

Takes a ``ClaimScope``. The underlying orchestrator doesn't consume the
per-run budget table — it loops up to ``max_rounds`` and stops. We map
``budgets.compute`` onto ``max_rounds`` (or fall back to the
orchestrator default).

Robustifier is an enumerator: every variant produced is part of the
deliverable. There's no finalize step that could starve, so
``incomplete`` here really means "raised" — we treat a clean return
with zero variants as ``complete`` with an empty delta (no variants
were produced, but the process ran its course).
"""

import logging
import time

from rumil.database import DB
from rumil.models import CallStatus, CallType
from rumil.orchestrators.robustify import DEFAULT_MAX_ROUNDS, RobustifyOrchestrator
from rumil.processes.budget import BudgetEnvelope, ResourceUsage
from rumil.processes.readback import assemble_variant_set_delta
from rumil.processes.result import Result
from rumil.processes.scope import ClaimScope
from rumil.processes.signals import FollowUp
from rumil.tracing.broadcast import Broadcaster
from rumil.tracing.trace_events import ProcessCompletedEvent, ProcessStartedEvent
from rumil.tracing.tracer import CallTrace

log = logging.getLogger(__name__)


class Robustifier:
    process_type = "robustifier"

    def __init__(self, db: DB, broadcaster: Broadcaster | None = None):
        self.db = db
        self.broadcaster = broadcaster

    async def run(self, scope: ClaimScope, budgets: BudgetEnvelope) -> Result:
        max_rounds = budgets.compute if budgets.compute is not None else DEFAULT_MAX_ROUNDS

        envelope_call = await self.db.create_call(
            CallType.PROCESS_ENVELOPE,
            scope_page_id=scope.claim_id,
        )
        envelope_trace = CallTrace(
            call_id=envelope_call.id, db=self.db, broadcaster=self.broadcaster
        )
        await envelope_trace.record(
            ProcessStartedEvent(
                process_type=self.process_type,
                scope=scope.model_dump(),
                budgets=budgets.model_dump(),
            )
        )

        orch = RobustifyOrchestrator(self.db, self.broadcaster, max_rounds=max_rounds)

        started = time.monotonic()
        status: str = "complete"
        self_report = ""
        variant_ids: list[str] = []
        try:
            returned = await orch.run(scope.claim_id)
            variant_ids = list(returned)
        except Exception as exc:
            log.exception("Robustifier run failed: %s", exc)
            status = "failed"
            self_report = f"orchestrator raised: {exc!r}"

        elapsed = time.monotonic() - started

        delta = await assemble_variant_set_delta(
            self.db, self.db.run_id, scope.claim_id, variant_ids
        )

        signals: list[FollowUp] = []

        usage = ResourceUsage(
            compute=max_rounds,
            writes=len(delta.new_pages) + len(delta.new_links) + len(delta.supersedes),
            wallclock_seconds=elapsed,
        )

        if not self_report:
            self_report = (
                f"robustified claim {scope.claim_id[:8]}: "
                f"{len(variant_ids)} variants over up to {max_rounds} rounds"
            )

        result = Result(
            process_type=self.process_type,
            run_id=self.db.run_id,
            delta=delta,
            signals=signals,
            usage=usage,
            status=status,
            self_report=self_report,
        )
        await envelope_trace.record(
            ProcessCompletedEvent(
                process_type=self.process_type,
                status=result.status,
                self_report=result.self_report,
                delta=result.delta.model_dump(),
                signals=[sig.model_dump() for sig in result.signals],
                usage=result.usage.model_dump(),
            )
        )
        await self.db.update_call_status(
            envelope_call.id,
            CallStatus.COMPLETE,
            result_summary=self_report,
        )
        return result
