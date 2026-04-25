"""Generative orchestrator: spec → refine → final artefact.

Drives the generator-refiner workflow end to end:

1. Create a hidden artefact-task question for the user's request.
2. Run generate_spec to produce the initial set of spec items.
3. Run refine_spec, which internally reads the spec + last-3 artefact/critique
   triples, edits the spec via add/supersede/delete, fires regenerate_and_critique
   to see how the artefact moves, and calls finalize_artefact when iteration
   is no longer worthwhile.
4. If the refiner hasn't finalized (e.g. budget ran out), the orchestrator
   falls back to force-finalizing the latest artefact so the caller always
   ends up with a visible artefact page.

Budget is allocated at the orchestrator level. generate_spec costs 1 unit;
each regenerate_and_critique inside refine_spec costs 2; refine_spec's own
agent-loop turns cost 1 each (run_agent_loop does not charge budget — budget
is only debited by explicit consume_budget calls, which the move does).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from rumil.calls.context_builders import active_spec_items_for_task
from rumil.calls.critique_artefact import CritiqueArtefactCall
from rumil.calls.critique_artefact_request_only import RequestOnlyCritiqueArtefactCall
from rumil.calls.generate_artefact import GenerateArtefactCall
from rumil.calls.generate_spec import GenerateSpecCall
from rumil.calls.refine_spec import RefineSpecCall
from rumil.database import DB
from rumil.models import (
    CallType,
    Page,
    PageLayer,
    PageType,
    Workspace,
)
from rumil.orchestrators.common import _create_broadcaster
from rumil.tracing.broadcast import Broadcaster

log = logging.getLogger(__name__)


@dataclass
class GenerativeResult:
    """Return value of GenerativeOrchestrator.run."""

    task_id: str
    artefact_id: str | None
    finalized: bool


class GenerativeOrchestrator:
    """Drive the generator-refiner loop to produce a visible artefact."""

    def __init__(
        self,
        db: DB,
        *,
        refine_max_rounds: int = 10,
        broadcaster: Broadcaster | None = None,
    ) -> None:
        self.db = db
        self.refine_max_rounds = refine_max_rounds
        self.broadcaster = broadcaster
        self._owns_broadcaster = False

    async def run(self, request: str, *, headline: str | None = None) -> GenerativeResult:
        """Produce an artefact for *request*. Returns the result's IDs.

        The orchestrator creates its own hidden artefact-task question,
        drives generate_spec + refine_spec to completion (or budget
        exhaustion), and ensures an artefact exists and is visible.
        """
        async with self._broadcaster_scope():
            task = await self._create_task(request, headline=headline)
            log.info(
                "Generative orchestrator: new run, task=%s, headline=%s",
                task.id[:8],
                task.headline[:70],
            )
            return await self._drive(task, skip_generate_spec=False)

    async def resume(self, task_id: str) -> GenerativeResult:
        """Resume work on an existing artefact task.

        Intended for recovering from an interrupted run: skips task creation
        (and skips generate_spec if spec items already exist), runs
        refine_spec with the current budget, and force-finalizes the latest
        artefact at the end. The refiner picks up prior iterations from the
        DB via RefinementContext — it sees the full history (current spec +
        last-N triples) and just has fresh agent-loop state.
        """
        async with self._broadcaster_scope():
            resolved_id = await self.db.resolve_page_id(task_id) if len(task_id) < 36 else task_id
            if not resolved_id:
                raise ValueError(f"resume: task {task_id} not found (could not resolve)")
            task = await self.db.get_page(resolved_id)
            if task is None:
                raise ValueError(f"resume: task {resolved_id} not found")
            if task.page_type != PageType.QUESTION:
                raise ValueError(
                    f"resume: task {resolved_id} is a {task.page_type.value}, expected question"
                )
            # Match the original project so new call rows land in the
            # right project_id scope. Task's project_id is canonical.
            if task.project_id and self.db.project_id != task.project_id:
                self.db.project_id = task.project_id

            existing_spec = await active_spec_items_for_task(task.id, self.db)
            log.info(
                "Generative orchestrator: resuming task=%s (existing spec items: %d)",
                task.id[:8],
                len(existing_spec),
            )
            return await self._drive(task, skip_generate_spec=bool(existing_spec))

    async def _drive(self, task: Page, *, skip_generate_spec: bool) -> GenerativeResult:
        """Shared inner loop used by both run() and resume()."""
        if not skip_generate_spec:
            await self._run_generate_spec(task.id)

        await self._run_refine_spec(task.id)

        artefact = await self.db.latest_artefact_for_task(task.id)
        finalized = False
        # Refiner never produced an artefact (budget exhausted before the
        # first regenerate_and_critique, or the model never fired one).
        # Try to produce one now with a direct generate+critique so the
        # caller at least has something.
        if artefact is None and await self.db.consume_budget(1):
            await self._one_off_regenerate(task.id, parent_call_id=None)
            artefact = await self.db.latest_artefact_for_task(task.id)

        if artefact is not None and artefact.hidden:
            await self.db.set_page_hidden(artefact.id, False)
            finalized = True
            log.info("Orchestrator force-finalized artefact %s", artefact.id[:8])
        elif artefact is not None and not artefact.hidden:
            finalized = True

        return GenerativeResult(
            task_id=task.id,
            artefact_id=artefact.id if artefact else None,
            finalized=finalized,
        )

    class _BroadcasterScope:
        """Context manager that lazily creates (and closes) a broadcaster."""

        def __init__(self, orchestrator: GenerativeOrchestrator) -> None:
            self._orch = orchestrator

        async def __aenter__(self) -> GenerativeOrchestrator._BroadcasterScope:
            if self._orch.broadcaster is None:
                self._orch.broadcaster = _create_broadcaster(self._orch.db)
                self._orch._owns_broadcaster = self._orch.broadcaster is not None
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            if self._orch._owns_broadcaster and self._orch.broadcaster is not None:
                await self._orch.broadcaster.close()

    def _broadcaster_scope(self) -> GenerativeOrchestrator._BroadcasterScope:
        return GenerativeOrchestrator._BroadcasterScope(self)

    async def _create_task(self, request: str, *, headline: str | None) -> Page:
        task = Page(
            page_type=PageType.QUESTION,
            layer=PageLayer.SQUIDGY,
            workspace=Workspace.RESEARCH,
            content=request,
            headline=headline or request[:120],
            hidden=True,
        )
        await self.db.save_page(task)
        return task

    async def _run_generate_spec(self, task_id: str) -> None:
        if not await self.db.consume_budget(1):
            log.warning("Budget exhausted before generate_spec could run")
            return
        call = await self.db.create_call(
            CallType.GENERATE_SPEC,
            scope_page_id=task_id,
        )
        runner = GenerateSpecCall(
            task_id,
            call,
            self.db,
            broadcaster=self.broadcaster,
        )
        await runner.run()

    async def _run_refine_spec(self, task_id: str) -> None:
        if not await self.db.consume_budget(1):
            log.warning("Budget exhausted before refine_spec could run")
            return
        call = await self.db.create_call(
            CallType.REFINE_SPEC,
            scope_page_id=task_id,
        )
        runner = RefineSpecCall(
            task_id,
            call,
            self.db,
            max_rounds=self.refine_max_rounds,
            broadcaster=self.broadcaster,
        )
        await runner.run()

    async def _one_off_regenerate(self, task_id: str, parent_call_id: str | None) -> None:
        """Run a single generate + request-only critique + workspace critique as a fallback.

        Each sub-call costs 1 budget; this method consumes incrementally and
        stops at whatever boundary budget allows (the artefact alone is the
        most important — critiques are a bonus signal here). Request-only
        critique runs before workspace-aware so the latter can build on it.
        """
        gen_call = await self.db.create_call(
            CallType.GENERATE_ARTEFACT,
            scope_page_id=task_id,
            parent_call_id=parent_call_id,
        )
        await GenerateArtefactCall(task_id, gen_call, self.db, broadcaster=self.broadcaster).run()

        if not await self.db.consume_budget(1):
            return
        ro_call = await self.db.create_call(
            CallType.CRITIQUE_ARTEFACT_REQUEST_ONLY,
            scope_page_id=task_id,
            parent_call_id=parent_call_id,
        )
        await RequestOnlyCritiqueArtefactCall(
            task_id, ro_call, self.db, broadcaster=self.broadcaster
        ).run()

        if not await self.db.consume_budget(1):
            return
        crit_call = await self.db.create_call(
            CallType.CRITIQUE_ARTEFACT,
            scope_page_id=task_id,
            parent_call_id=parent_call_id,
        )
        await CritiqueArtefactCall(task_id, crit_call, self.db, broadcaster=self.broadcaster).run()


async def run_generative_workflow(
    db: DB,
    request: str,
    *,
    headline: str | None = None,
    refine_max_rounds: int = 10,
) -> GenerativeResult:
    """One-shot convenience wrapper for the generative orchestrator."""
    orchestrator = GenerativeOrchestrator(db, refine_max_rounds=refine_max_rounds)
    return await orchestrator.run(request, headline=headline)
