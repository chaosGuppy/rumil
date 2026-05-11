"""Subroutine library for SimpleSpine.

Each subroutine is a named, fingerprintable spec of a thing the mainline
agent can spawn. Four kinds:

- :class:`FreeformAgentSubroutine` — generic agent loop with tools.
- :class:`SampleNSubroutine` — fire one prompt N times in parallel.
- :class:`CallTypeSubroutine` — wrap an existing rumil ``CallRunner``;
  runs in a per-spawn staged sub-DB so its workspace writes don't leak.
- :class:`NestedOrchSubroutine` — recurse into another orchestrator
  (TwoPhase / DraftAndEdit / SimpleSpine itself) with a carved sub-budget.

Each subroutine declares ``overridable`` — the whitelist of fields the
mainline agent may override at spawn time. The spawn tool's input
schema is generated from that whitelist.
"""

from rumil.orchestrators.simple_spine.subroutines.base import (
    ConfigPrepDef,
    SpawnCtx,
    SubroutineDef,
    SubroutineResult,
)
from rumil.orchestrators.simple_spine.subroutines.call_type import CallTypeSubroutine
from rumil.orchestrators.simple_spine.subroutines.freeform_agent import (
    FreeformAgentPreppedConfig,
    FreeformAgentSubroutine,
)
from rumil.orchestrators.simple_spine.subroutines.nested_orch import (
    NestedOrchFactory,
    NestedOrchSubroutine,
)
from rumil.orchestrators.simple_spine.subroutines.sample_n import SampleNSubroutine
from rumil.orchestrators.simple_spine.subroutines.web_research import WebResearchSubroutine

__all__ = (
    "CallTypeSubroutine",
    "ConfigPrepDef",
    "FreeformAgentPreppedConfig",
    "FreeformAgentSubroutine",
    "NestedOrchFactory",
    "NestedOrchSubroutine",
    "SampleNSubroutine",
    "SpawnCtx",
    "SubroutineDef",
    "SubroutineResult",
    "WebResearchSubroutine",
)
