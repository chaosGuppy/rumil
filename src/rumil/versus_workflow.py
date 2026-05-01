"""Workflow protocol for plugging rumil orchestrators into versus tasks.

Versus needs to drive different rumil "shapes of work" through one
interface — today, the orch judge path runs ``TwoPhaseOrchestrator`` per
pair; later, completion tasks (#426/#427) will run draft-and-edit or
similar over the same harness. Each shape is a ``Workflow``.

The protocol is deliberately thin — ``setup`` seeds the budget,
``run`` does the work, ``fingerprint`` describes the workflow's
behaviour-affecting knobs for dedup keys. ``code_paths`` is declared
on the protocol now and consumed in #425 when per-workflow code
fingerprinting lands. ``produces_artifact`` flags whether the closer
should read ``question.content`` verbatim (artifact workflows like
DraftAndEdit) or extract a label from the research subgraph
(research workflows like TwoPhase).

Invariants worth preserving in subclasses:

- ``setup`` only touches ``db`` (e.g. ``init_budget``). Idempotent —
  versus rerun mode may skip it when the budget is already seeded.
- ``run`` is where async work + tracing spans happen. Keeping it pure
  execution lets the bridge / harness wrap timing, retries, langfuse
  spans without disturbing setup.
- ``fingerprint()`` returns workflow-specific knobs only. Global
  settings that affect behaviour (assess_call_variant, etc.) live in
  the run config; #424 will fold them in via ``make_versus_config``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol, runtime_checkable

from rumil.database import DB
from rumil.orchestrators.two_phase import TwoPhaseOrchestrator
from rumil.settings import get_settings
from rumil.tracing.broadcast import Broadcaster


@runtime_checkable
class Workflow(Protocol):
    """One pluggable shape of work versus can fire on a question."""

    name: str
    code_paths: Sequence[str]
    produces_artifact: bool

    def fingerprint(self) -> Mapping[str, str | int | bool | None]: ...

    async def setup(self, db: DB, question_id: str) -> None: ...

    async def run(self, db: DB, question_id: str, broadcaster: Broadcaster | None) -> None: ...


class _BudgetedOrchWorkflow:
    """Shared base for orchestrators that take ``budget_cap=N``.

    Captures the ``db.init_budget(budget) -> Orch(db, broadcaster,
    budget_cap=budget).run(qid)`` shape used by TwoPhase / Experimental
    / ClaimInvestigation. Subclasses set ``name``, ``code_paths``, and
    ``orch_cls``.

    ``produces_artifact`` defaults to ``False`` — these orchestrators
    leave their output as a research subgraph (considerations, claims,
    judgements, refreshed View), and the closer extracts the label.

    ``relevant_settings`` lists names of :class:`rumil.settings.Settings`
    fields whose values affect this workflow's *behaviour* (which calls
    dispatch, which moves are available, etc.). Their current values
    are folded into :meth:`fingerprint` so an orchestrator-level setting
    flip auto-forks the dedup key. Settings that affect *prompt content*
    are deliberately not listed — the code fingerprint over the
    prompts directory already covers them.
    """

    name: str = "<override>"
    code_paths: Sequence[str] = ()
    produces_artifact: bool = False
    orch_cls: type
    relevant_settings: Sequence[str] = (
        # which assess implementation runs (default vs big context).
        "assess_call_variant",
        # which moves the orchestrator can dispatch.
        "available_moves",
        "available_calls",
        # whether prioritization runs the red-team pass.
        "enable_red_team",
        # global prio knobs gate whether and when global prioritization fires.
        "enable_global_prio",
        # subquestion linker on/off changes which links the orch creates.
        "subquestion_linker_enabled",
        # which prioritization variant runs end-to-end.
        "prioritizer_variant",
        # view variant changes how the closer reads the orch's output.
        "view_variant",
        # budget pacing affects how the orch spends its budget.
        "budget_pacing_enabled",
    )

    def __init__(self, *, budget: int) -> None:
        self.budget = budget

    def fingerprint(self) -> Mapping[str, str | int | bool | None]:
        settings = get_settings()
        snap: dict[str, str | int | bool | None] = {
            f"settings.{k}": getattr(settings, k) for k in self.relevant_settings
        }
        return {"kind": self.name, "budget": self.budget, **snap}

    async def setup(self, db: DB, question_id: str) -> None:
        await db.init_budget(self.budget)

    async def run(self, db: DB, question_id: str, broadcaster: Broadcaster | None) -> None:
        orch = self.orch_cls(db=db, broadcaster=broadcaster, budget_cap=self.budget)
        await orch.run(question_id)


class TwoPhaseWorkflow(_BudgetedOrchWorkflow):
    """Versus workflow wrapping :class:`TwoPhaseOrchestrator`.

    ``code_paths`` covers everything an in-flight TwoPhase run can
    touch — the orchestrator modules it directly composes, plus the
    full ``calls/`` and ``prompts/`` directories since the orch
    dispatches across many call types and any of their prompts can
    affect output. Bias is per-workflow over-inclusion (issue #425):
    a stale prompt edit forking only TwoPhase rows is cheap; missing
    it would silently let a content-changing edit slip past dedup.
    """

    name = "two_phase"
    code_paths: Sequence[str] = (
        "src/rumil/orchestrators/two_phase.py",
        "src/rumil/orchestrators/base.py",
        "src/rumil/orchestrators/common.py",
        "src/rumil/orchestrators/dispatch_handlers.py",
        # Calls dispatched during research (assess, find_considerations,
        # the scout family, web_research, view, etc.). Per-workflow over-
        # inclusion is cheap; per-call cherry-picking is fragile.
        "src/rumil/calls/",
        # Per-call prompts. ``preamble.md`` is shared (lives on the
        # cross-cutting fingerprint) but every other prompt under this
        # directory is call-specific and forks if edited.
        "src/rumil/prompts/",
    )
    produces_artifact = False
    orch_cls = TwoPhaseOrchestrator
