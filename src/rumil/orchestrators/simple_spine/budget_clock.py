"""Budget clock for SimpleSpine: tracks tokens (hard cap) + wall-clock (soft).

The clock is queried every round to surface remaining headroom to the
mainline agent and to decide whether to force-finalize. ``record_tokens``
is called by every LLM-touching step (mainline turns, subroutine LLM
calls). ``snapshot`` returns a render-ready summary for the prompt.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class BudgetSpec:
    """Caller-supplied budget envelope for one SimpleSpine run.

    ``max_tokens`` is the only hard cap — when crossed, no further spawns
    are allowed and the next mainline turn is nudged to finalize.

    ``wall_clock_soft_s`` is surfaced to the agent as a remaining-time
    signal but never triggers automatic abort. The agent decides what to
    do with the budget.
    """

    max_tokens: int
    wall_clock_soft_s: float | None = None


@dataclass
class BudgetSnapshot:
    """Render-ready view of clock state for prompt construction."""

    tokens_used: int
    tokens_remaining: int
    elapsed_s: float
    wall_clock_soft_s: float | None
    tokens_exhausted: bool


@dataclass
class BudgetClock:
    """Mutable accumulator threaded through a SimpleSpine run.

    Carve-from-parent recursion is supported via :meth:`carve_child`,
    which returns a child clock backed by this clock's accounting plus
    its own sub-cap. The child's spend debits both itself and the parent;
    the parent never sees the child's wall-clock.
    """

    spec: BudgetSpec
    tokens_used: int = 0
    started_at: float = field(default_factory=time.monotonic)
    _parent: BudgetClock | None = None

    def record_tokens(self, n: int) -> None:
        """Add ``n`` tokens (input+output combined) to the running total."""
        if n <= 0:
            return
        self.tokens_used += n
        if self._parent is not None:
            self._parent.record_tokens(n)

    @property
    def tokens_remaining(self) -> int:
        return max(self.spec.max_tokens - self.tokens_used, 0)

    @property
    def tokens_exhausted(self) -> bool:
        return self.tokens_used >= self.spec.max_tokens

    @property
    def elapsed_s(self) -> float:
        return time.monotonic() - self.started_at

    def snapshot(self) -> BudgetSnapshot:
        return BudgetSnapshot(
            tokens_used=self.tokens_used,
            tokens_remaining=self.tokens_remaining,
            elapsed_s=self.elapsed_s,
            wall_clock_soft_s=self.spec.wall_clock_soft_s,
            tokens_exhausted=self.tokens_exhausted,
        )

    def carve_child(self, sub_token_cap: int) -> BudgetClock:
        """Carve a child clock with its own sub-cap, debiting this clock.

        The child's ``record_tokens`` flows up to this clock too, so
        nested orchs cannot exceed the parent's hard cap regardless of
        how their own ``max_tokens`` is set.
        """
        if sub_token_cap <= 0:
            raise ValueError(f"sub_token_cap must be > 0, got {sub_token_cap}")
        capped = min(sub_token_cap, self.tokens_remaining)
        child_spec = BudgetSpec(
            max_tokens=capped,
            wall_clock_soft_s=self.spec.wall_clock_soft_s,
        )
        return BudgetClock(spec=child_spec, _parent=self)

    def render_for_prompt(self) -> str:
        """Compact human/agent-readable summary for system reminders."""
        s = self.snapshot()
        parts = [
            f"tokens: {s.tokens_used:,} / {self.spec.max_tokens:,} "
            f"(remaining {s.tokens_remaining:,})",
        ]
        if s.wall_clock_soft_s is not None:
            parts.append(f"wall-clock: {s.elapsed_s:.0f}s / {s.wall_clock_soft_s:.0f}s soft")
        else:
            parts.append(f"wall-clock: {s.elapsed_s:.0f}s")
        return " | ".join(parts)
