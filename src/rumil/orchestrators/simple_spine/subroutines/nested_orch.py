"""NestedOrchSubroutine — recurse into another orchestrator with a sub-budget.

Two flavors are supported via the ``orch_factory`` callable:

- A budgeted research orch (TwoPhase / ClaimInvestigation) that takes
  ``assigned_budget`` in **rumil budget units** (one unit per call).
  The token sub-cap carved from the parent's BudgetClock here is a
  separate axis — set ``budget_units`` on the override or via the
  factory's defaults.
- A SimpleSpine recursion (factory returns a ``SimpleSpineOrchestrator``).
  The token sub-cap is the only knob; the child orch lives entirely
  inside our token clock.

The factory receives ``(ctx, sub_token_cap, overrides)`` and returns a
ready-to-await coroutine that finishes the sub-orch run. This keeps the
SubroutineDef agnostic about which orch shape it wraps.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from rumil.orchestrators.simple_spine.subroutines.base import (
    ConfigPrepDef,
    SpawnCtx,
    SubroutineResult,
)


def _sha8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


# Factory: (ctx, sub_token_cap, overrides) -> awaitable returning a text
# summary that bubbles back to mainline.
NestedOrchFactory = Callable[
    [SpawnCtx, int, Mapping[str, Any]],
    Awaitable[str],
]


@dataclass(frozen=True)
class NestedOrchSubroutine:
    name: str
    description: str
    orch_kind: str  # "two_phase" | "draft_and_edit" | "simple_spine" | etc.
    factory: NestedOrchFactory
    base_token_cap: int
    overridable: frozenset[str] = field(
        default_factory=lambda: frozenset({"intent", "additional_context"})
    )
    config_prep: ConfigPrepDef | None = None
    cost_hint: str | None = None

    def fingerprint(self) -> Mapping[str, Any]:
        out: dict[str, Any] = {
            "kind": "nested_orch",
            "name": self.name,
            "orch_kind": self.orch_kind,
            "base_token_cap": self.base_token_cap,
            "overridable": sorted(self.overridable),
        }
        if self.config_prep is not None:
            out["config_prep"] = self.config_prep.fingerprint()
        return out

    def spawn_tool_schema(self) -> dict[str, Any]:
        properties: dict[str, Any] = {
            "intent": {
                "type": "string",
                "description": "What you want this nested orch to investigate / produce.",
            },
        }
        required = ["intent"]
        if "additional_context" in self.overridable:
            properties["additional_context"] = {
                "type": "string",
                "description": "Context to forward to the nested orch.",
            }
        if "token_cap" in self.overridable:
            properties["token_cap"] = {
                "type": "integer",
                "minimum": 1000,
                "description": (
                    "Override the token sub-cap (default "
                    f"{self.base_token_cap}). Capped at parent's remaining."
                ),
            }
        return {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }

    async def run(self, ctx: SpawnCtx, overrides: Mapping[str, Any]) -> SubroutineResult:
        cap_override = overrides.get("token_cap")
        sub_cap = (
            int(cap_override)
            if cap_override is not None and "token_cap" in self.overridable
            else self.base_token_cap
        )
        # carve_child clamps to the parent's remaining; if the parent is
        # already drained we fall back to a token-1 sub-cap so the call
        # is well-formed and immediately reports tokens_exhausted.
        sub_cap = max(min(sub_cap, ctx.budget_clock.tokens_remaining), 1)

        before = ctx.budget_clock.tokens_used
        text_summary = await self.factory(ctx, sub_cap, overrides)
        consumed = ctx.budget_clock.tokens_used - before
        return SubroutineResult(
            text_summary=text_summary,
            extra={
                "nested_orch_kind": self.orch_kind,
                "sub_token_cap": sub_cap,
                "tokens_consumed": consumed,
            },
        )
