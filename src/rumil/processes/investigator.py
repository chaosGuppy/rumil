"""Investigator process: wraps TwoPhaseOrchestrator to produce a ViewDelta.

Takes a ``QuestionScope`` and a ``BudgetEnvelope``. Instantiates
``TwoPhaseOrchestrator`` with ``budget_cap = budgets.compute`` and runs
it. After the underlying run completes, reads back pages/links tagged
with ``db.run_id`` and assembles a ``ViewDelta`` — picking out the
synthesis View page by ``VIEW_OF`` link where possible.

Completion semantics:

- ``complete`` iff the orchestrator returned cleanly and a View page
  was produced for the scope question.
- ``incomplete`` if the orchestrator returned cleanly but no View was
  produced (budget ran out before synthesis, or the question didn't
  reach the point of View creation).
- ``failed`` if the orchestrator raised.

v1 emits no signals. Signals will become load-bearing when Surveyor
work lands; the Investigator wrap is intentionally signal-quiet.
"""

import logging
import time

from rumil.database import DB
from rumil.models import CallStatus, CallType
from rumil.orchestrators.two_phase import TwoPhaseOrchestrator
from rumil.processes.budget import BudgetEnvelope, ResourceUsage
from rumil.processes.readback import assemble_view_delta
from rumil.processes.result import Result
from rumil.processes.scope import QuestionScope
from rumil.processes.signals import FollowUp
from rumil.tracing.broadcast import Broadcaster
from rumil.tracing.trace_events import ProcessCompletedEvent, ProcessStartedEvent
from rumil.tracing.tracer import CallTrace

log = logging.getLogger(__name__)

DEFAULT_COMPUTE = 10


class Investigator:
    process_type = "investigator"

    def __init__(self, db: DB, broadcaster: Broadcaster | None = None):
        self.db = db
        self.broadcaster = broadcaster

    async def run(self, scope: QuestionScope, budgets: BudgetEnvelope) -> Result:
        compute_cap = budgets.compute if budgets.compute is not None else DEFAULT_COMPUTE

        envelope_call = await self.db.create_call(
            CallType.PROCESS_ENVELOPE,
            scope_page_id=scope.question_id,
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

        orch = TwoPhaseOrchestrator(self.db, self.broadcaster, budget_cap=compute_cap)

        started = time.monotonic()
        status: str = "complete"
        self_report = ""
        try:
            await orch.run(scope.question_id)
        except Exception as exc:
            log.exception("Investigator run failed: %s", exc)
            status = "failed"
            self_report = f"orchestrator raised: {exc!r}"
        elapsed = time.monotonic() - started

        delta = await assemble_view_delta(self.db, self.db.run_id, scope.question_id)

        if status == "complete" and delta.view_page_id is None:
            status = "incomplete"
            self_report = self_report or "no View page produced before stopping"

        signals: list[FollowUp] = []

        usage = ResourceUsage(
            compute=orch._consumed,
            writes=len(delta.new_pages) + len(delta.new_links) + len(delta.supersedes),
            wallclock_seconds=elapsed,
        )

        if not self_report:
            view_hint = f"View {delta.view_page_id[:8]}" if delta.view_page_id else "no View"
            self_report = (
                f"investigated question {scope.question_id[:8]}: "
                f"{len(delta.new_pages)} pages, {len(delta.new_links)} links, {view_hint}"
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
