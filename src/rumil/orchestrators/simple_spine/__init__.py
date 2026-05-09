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
from rumil.orchestrators.simple_spine.loader import (
    discover_configs,
    load_simple_spine_config,
)
from rumil.orchestrators.simple_spine.orchestrator import SimpleSpineOrchestrator
from rumil.orchestrators.simple_spine.presets import (
    get_preset,
    list_presets,
    register_preset,
)
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
    register_tool,
    resolve_tools,
)
from rumil.orchestrators.simple_spine.validators import (
    list_validators,
    register_validator,
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
    "discover_configs",
    "get_preset",
    "list_presets",
    "list_validators",
    "load_simple_spine_config",
    "make_finalize_tool",
    "register_preset",
    "register_tool",
    "register_validator",
    "resolve_tools",
)
