"""SimpleSpine orchestrator — structured-rounds main loop with parallel spawns.

Public API:

- :class:`SimpleSpineConfig` / :class:`OrchInputs` / :class:`OrchResult`
  — the data the orch is parameterised by and what it returns.
- :class:`SimpleSpineOrchestrator` — the runtime; call ``run(inputs)``.
- :class:`SimpleSpineWorkflow` — versus :class:`Workflow` adapter
  (``produces_artifact=True``).
- :class:`BudgetSpec` / :class:`BudgetClock` — token + wall-clock
  accounting threaded through every spawn.
- The four ``SubroutineDef`` kinds — :class:`FreeformAgentSubroutine`,
  :class:`SampleNSubroutine`, :class:`CallTypeSubroutine`,
  :class:`NestedOrchSubroutine` — plus :class:`ConfigPrepDef` for the
  optional two-phase spawn pattern.
"""

from rumil.orchestrators.simple_spine.budget_clock import (
    BudgetClock,
    BudgetSnapshot,
    BudgetSpec,
)
from rumil.orchestrators.simple_spine.config import (
    OrchInputs,
    OrchResult,
    SimpleSpineConfig,
)
from rumil.orchestrators.simple_spine.orchestrator import SimpleSpineOrchestrator
from rumil.orchestrators.simple_spine.subroutines import (
    CallTypeSubroutine,
    ConfigPrepDef,
    FreeformAgentPreppedConfig,
    FreeformAgentSubroutine,
    NestedOrchFactory,
    NestedOrchSubroutine,
    SampleNSubroutine,
    SpawnCtx,
    SubroutineDef,
    SubroutineResult,
)
from rumil.orchestrators.simple_spine.tools import (
    make_finalize_tool,
    make_note_finding_tool,
    register_tool,
    resolve_tools,
)
from rumil.orchestrators.simple_spine.workflow import SimpleSpineWorkflow

__all__ = (
    "BudgetClock",
    "BudgetSnapshot",
    "BudgetSpec",
    "CallTypeSubroutine",
    "ConfigPrepDef",
    "FreeformAgentPreppedConfig",
    "FreeformAgentSubroutine",
    "NestedOrchFactory",
    "NestedOrchSubroutine",
    "OrchInputs",
    "OrchResult",
    "SampleNSubroutine",
    "SimpleSpineConfig",
    "SimpleSpineOrchestrator",
    "SimpleSpineWorkflow",
    "SpawnCtx",
    "SubroutineDef",
    "SubroutineResult",
    "make_finalize_tool",
    "make_note_finding_tool",
    "register_tool",
    "resolve_tools",
)
