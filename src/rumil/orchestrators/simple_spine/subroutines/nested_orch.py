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

import dataclasses
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from rumil.orchestrators.simple_spine.subroutines.base import (
    SpawnCtx,
    SubroutineBase,
    SubroutineResult,
)

# Factory: (ctx, sub_token_cap, overrides) -> awaitable returning a text
# summary that bubbles back to mainline.
NestedOrchFactory = Callable[
    [SpawnCtx, int, Mapping[str, Any]],
    Awaitable[str],
]


@dataclass(frozen=True, kw_only=True)
class NestedOrchSubroutine(SubroutineBase):
    """Recurse into another orchestrator with a carved sub-budget.

    Inherits cross-cutting fields from :class:`SubroutineBase`. Honors
    ``inherit_assumptions`` by gating whether ``ctx.operating_assumptions``
    is forwarded to the nested orch's factory. Treats
    ``base_token_cap`` as **required** (validated in ``__post_init__``)
    because nested orchs can recurse arbitrarily deep without it.
    """

    orch_kind: str  # "two_phase" | "draft_and_edit" | "simple_spine" | etc.
    factory: NestedOrchFactory
    overridable: frozenset[str] = field(
        default_factory=lambda: frozenset({"intent", "additional_context"})
    )

    def __post_init__(self) -> None:
        if self.base_token_cap is None:
            raise ValueError(
                f"NestedOrchSubroutine {self.name!r}: base_token_cap is "
                "required (nested orchs always need an explicit token "
                "sub-cap because they can recurse arbitrarily deep)"
            )
        if self.consumes:
            raise ValueError(
                f"NestedOrchSubroutine {self.name!r}: consumes is not yet "
                "supported on nested_orch kinds — artifact pass-through to "
                "a child orch needs an explicit forwarding contract that "
                "is out of MVP scope. Track which keys the child orch "
                "needs via its own OrchInputs.artifacts at factory time."
            )

    def _supports_include_artifacts(self) -> bool:
        return False

    def fingerprint(self) -> Mapping[str, Any]:
        out = dict(super().fingerprint())
        out["kind"] = "nested_orch"
        out["orch_kind"] = self.orch_kind
        return out

    def _default_intent_description(self) -> str:
        return "What you want this nested orch to investigate / produce."

    def _default_additional_context_description(self) -> str:
        return "Context to forward to the nested orch."

    def _token_cap_property(self) -> dict[str, Any]:
        return {
            "type": "integer",
            "minimum": 1000,
            "description": (
                "Override the token sub-cap (default "
                f"{self.base_token_cap}). Capped at parent's remaining."
            ),
        }

    async def run(self, ctx: SpawnCtx, overrides: Mapping[str, Any]) -> SubroutineResult:
        cap_override = overrides.get("token_cap")
        # base_token_cap is enforced non-None in __post_init__; assert keeps
        # pyright happy without re-validating at every spawn.
        assert self.base_token_cap is not None
        sub_cap = (
            int(cap_override)
            if cap_override is not None and "token_cap" in self.overridable
            else self.base_token_cap
        )
        # carve_child clamps to the parent's remaining; if the parent is
        # already drained we fall back to a token-1 sub-cap so the call
        # is well-formed and immediately reports tokens_exhausted.
        sub_cap = max(min(sub_cap, ctx.budget_clock.tokens_remaining), 1)

        # Honor inherit_assumptions by zeroing operating_assumptions on
        # the ctx forwarded to the factory. Default-True so global rules
        # propagate; opt-out for nested orchs whose role is to push back.
        forward_ctx = (
            ctx if self.inherit_assumptions else dataclasses.replace(ctx, operating_assumptions="")
        )
        text_summary = await self.factory(forward_ctx, sub_cap, overrides)
        # tokens_consumed is reported by the orchestrator from the
        # per-spawn BudgetClock (carved via SubroutineBase.carve_spawn_clock);
        # no need for a manual delta here.
        return SubroutineResult(
            text_summary=text_summary,
            extra={
                "nested_orch_kind": self.orch_kind,
                "sub_token_cap": sub_cap,
            },
        )
