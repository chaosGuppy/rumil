"""Budget clock for SimpleSpine: tracks USD cost (hard cap) + wall-clock (soft).

The clock is queried every round to surface remaining headroom to the
mainline agent and to decide whether to force-finalize. ``record_exchange``
is called by every LLM-touching step (mainline turns, subroutine LLM
calls); cost is computed via :func:`rumil.pricing.compute_cost`, so all
four token classes (input, output, cache_create, cache_read) hit the
budget at their real per-model rates. ``snapshot`` returns a render-ready
summary for the prompt.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from rumil.pricing import compute_cost


def _aggregate_usage_full(usage: Any) -> tuple[int, int, int, int]:
    """Return (input, output, cache_creation, cache_read) summed across compaction iterations.

    Mirrors :func:`rumil.llm._aggregate_usage_tokens` but extends the
    return shape to all four token classes — needed because the budget
    is now USD, and cost depends on every class. When ``usage.iterations``
    is absent (no compaction this request) the top-level fields are
    accurate; otherwise sum across.
    """
    iterations = getattr(usage, "iterations", None) or []
    if not iterations:
        return (
            getattr(usage, "input_tokens", 0) or 0,
            getattr(usage, "output_tokens", 0) or 0,
            getattr(usage, "cache_creation_input_tokens", 0) or 0,
            getattr(usage, "cache_read_input_tokens", 0) or 0,
        )
    total_in = sum((getattr(it, "input_tokens", 0) or 0) for it in iterations)
    total_out = sum((getattr(it, "output_tokens", 0) or 0) for it in iterations)
    total_cc = sum((getattr(it, "cache_creation_input_tokens", 0) or 0) for it in iterations)
    total_cr = sum((getattr(it, "cache_read_input_tokens", 0) or 0) for it in iterations)
    return (total_in, total_out, total_cc, total_cr)


@dataclass
class BudgetSpec:
    """Caller-supplied budget envelope for one SimpleSpine run.

    ``max_cost_usd`` is the only hard cap — when crossed, no further
    spawns are allowed and the next mainline turn is nudged to finalize.
    Cost includes input + output + cache_create + cache_read at the
    per-model rates from ``pricing.json``, so cache-write spend (which
    used to fly under a token-only budget) is properly bounded.

    ``wall_clock_soft_s`` is surfaced to the agent as a remaining-time
    signal but never triggers automatic abort. The agent decides what to
    do with the budget.
    """

    max_cost_usd: float
    wall_clock_soft_s: float | None = None


@dataclass
class BudgetSnapshot:
    """Render-ready view of clock state for prompt construction."""

    cost_usd_used: float
    cost_usd_remaining: float
    elapsed_s: float
    wall_clock_soft_s: float | None
    cost_exhausted: bool


@dataclass
class BudgetClock:
    """Mutable accumulator threaded through a SimpleSpine run.

    Carve-from-parent recursion is supported via :meth:`carve_child`,
    which returns a child clock backed by this clock's accounting plus
    its own sub-cap. The child's spend debits both itself and the parent;
    the parent never sees the child's wall-clock.
    """

    spec: BudgetSpec
    cost_usd_used: float = 0.0
    started_at: float = field(default_factory=time.monotonic)
    _parent: BudgetClock | None = None

    def record_exchange(self, usage: Any, model: str) -> None:
        """Add the cost of one LLM exchange to the running total.

        Computes cost via :func:`rumil.pricing.compute_cost`, then
        propagates upward to any parent clock so a nested orch can never
        exceed its parent's hard cap regardless of its own carve.
        """
        in_tok, out_tok, cc_tok, cr_tok = _aggregate_usage_full(usage)
        cost = compute_cost(
            model=model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cache_creation_input_tokens=cc_tok,
            cache_read_input_tokens=cr_tok,
        )
        self._record_cost(cost)

    def _record_cost(self, cost_usd: float) -> None:
        if cost_usd <= 0:
            return
        self.cost_usd_used += cost_usd
        if self._parent is not None:
            self._parent._record_cost(cost_usd)

    @property
    def cost_usd_remaining(self) -> float:
        return max(self.spec.max_cost_usd - self.cost_usd_used, 0.0)

    @property
    def cost_exhausted(self) -> bool:
        return self.cost_usd_used >= self.spec.max_cost_usd

    @property
    def elapsed_s(self) -> float:
        return time.monotonic() - self.started_at

    def snapshot(self) -> BudgetSnapshot:
        return BudgetSnapshot(
            cost_usd_used=self.cost_usd_used,
            cost_usd_remaining=self.cost_usd_remaining,
            elapsed_s=self.elapsed_s,
            wall_clock_soft_s=self.spec.wall_clock_soft_s,
            cost_exhausted=self.cost_exhausted,
        )

    def carve_child(self, sub_cost_cap_usd: float) -> BudgetClock:
        """Carve a child clock with its own sub-cap, debiting this clock.

        The child's ``record_exchange`` flows up to this clock too, so
        nested orchs cannot exceed the parent's hard cap regardless of
        how their own ``max_cost_usd`` is set.
        """
        if sub_cost_cap_usd <= 0:
            raise ValueError(f"sub_cost_cap_usd must be > 0, got {sub_cost_cap_usd}")
        capped = min(sub_cost_cap_usd, self.cost_usd_remaining)
        child_spec = BudgetSpec(
            max_cost_usd=capped,
            wall_clock_soft_s=self.spec.wall_clock_soft_s,
        )
        return BudgetClock(spec=child_spec, _parent=self)

    def render_for_prompt(self) -> str:
        """Compact human/agent-readable summary for system reminders."""
        s = self.snapshot()
        parts = [
            f"budget: ${s.cost_usd_used:.2f} / ${self.spec.max_cost_usd:.2f} "
            f"(remaining ${s.cost_usd_remaining:.2f})",
        ]
        if s.wall_clock_soft_s is not None:
            parts.append(f"wall-clock: {s.elapsed_s:.0f}s / {s.wall_clock_soft_s:.0f}s soft")
        else:
            parts.append(f"wall-clock: {s.elapsed_s:.0f}s")
        return " | ".join(parts)
