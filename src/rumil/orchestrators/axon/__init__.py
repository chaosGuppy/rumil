"""Axon orchestrator.

A cache-aware research orchestrator built around one mainline-facing
primitive — `delegate` — and a two-step dispatch flow:

1. Mainline emits ``delegate(intent, inherit_context, budget_usd, n=1)``
   tool calls (one or many in parallel per turn).
2. Each is followed by a ``configure`` continuation in the same thread
   that produces a structured ``DelegateConfig`` (system prompt, tools,
   max rounds, finalize schema, side effects, ...).
3. The orchestrator runs each delegate's inner loop with its config;
   inner loops terminate by calling the universal ``finalize`` tool.
4. All parallel results are gathered before the next mainline turn.

Two regimes for context inheritance, mutually exclusive by design:

- *Continuation* (``inherit_context=True``): inner loop reuses the
  spine's system + tools + messages — cache-shared prefix. Configure
  must not customise system or tools.
- *Isolation* (``inherit_context=False``): inner loop starts fresh;
  configure picks any system / tools.

Mainline tool surface is intentionally tiny and stable across the run:
``delegate``, ``configure``, ``finalize``, ``load_page``. Multi-round
work (web research, workspace search, etc.) lives inside delegates.
"""

from __future__ import annotations

from rumil.orchestrators.axon.artifacts import Artifact, ArtifactSeed, ArtifactStore
from rumil.orchestrators.axon.budget_clock import BudgetClock, BudgetSnapshot, BudgetSpec
from rumil.orchestrators.axon.config import (
    OPERATING_ASSUMPTIONS_KEY,
    AxonConfig,
    OrchInputs,
    OrchResult,
    build_initial_artifacts,
)
from rumil.orchestrators.axon.direct_tools import (
    DirectToolCtx,
    direct_tool_ctx_scope,
    get_direct_tool_ctx,
    set_direct_tool_ctx,
)
from rumil.orchestrators.axon.loader import discover_configs, load_axon_config
from rumil.orchestrators.axon.orchestrator import AxonOrchestrator
from rumil.orchestrators.axon.runner import (
    InnerLoopResult,
    run_inner_loop,
    validate_finalize_payload,
)
from rumil.orchestrators.axon.schemas import (
    DelegateConfig,
    DelegateRequest,
    FinalizeSchemaSpec,
    SideEffect,
    SystemPromptSpec,
)
from rumil.orchestrators.axon.tools import (
    CONFIGURE_TOOL_NAME,
    DELEGATE_TOOL_NAME,
    FINALIZE_TOOL_NAME,
    build_configure_tool,
    build_delegate_tool,
    build_finalize_tool,
    build_mainline_tools,
    register_direct_tool,
    resolve_direct_tools,
)
from rumil.orchestrators.axon.workflow import AxonWorkflow

__all__ = (
    "CONFIGURE_TOOL_NAME",
    "DELEGATE_TOOL_NAME",
    "FINALIZE_TOOL_NAME",
    "OPERATING_ASSUMPTIONS_KEY",
    "Artifact",
    "ArtifactSeed",
    "ArtifactStore",
    "AxonConfig",
    "AxonOrchestrator",
    "AxonWorkflow",
    "BudgetClock",
    "BudgetSnapshot",
    "BudgetSpec",
    "DelegateConfig",
    "DelegateRequest",
    "DirectToolCtx",
    "FinalizeSchemaSpec",
    "InnerLoopResult",
    "OrchInputs",
    "OrchResult",
    "SideEffect",
    "SystemPromptSpec",
    "build_configure_tool",
    "build_delegate_tool",
    "build_finalize_tool",
    "build_initial_artifacts",
    "build_mainline_tools",
    "direct_tool_ctx_scope",
    "discover_configs",
    "get_direct_tool_ctx",
    "load_axon_config",
    "register_direct_tool",
    "resolve_direct_tools",
    "run_inner_loop",
    "set_direct_tool_ctx",
    "validate_finalize_payload",
)
