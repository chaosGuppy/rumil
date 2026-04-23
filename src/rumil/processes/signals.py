"""FollowUp: typed recommendations a process emits for a scheduler (or human).

Signals are *recommendations*, not commitments. A process emits signals
to suggest what should happen next; something else decides whether to
act on them. No process in v1 has a scheduler consuming these — they're
emitted for observability and for the rewrite of Surveyor-shaped work,
where the natural output is primarily recommendations rather than
mutations.

Each signal carries a ``reason`` (free text) and any fields a consumer
would need to act on it without re-deriving context.
"""

from typing import Literal

from pydantic import BaseModel, Field


class _SignalBase(BaseModel):
    reason: str = ""


class FocusRequest(_SignalBase):
    """Recommend that a question deserves more investigation."""

    kind: Literal["focus"] = "focus"
    question_id: str
    priority: float | None = Field(
        default=None,
        description="Suggested priority 0-1; None = unscored",
    )
    suggested_budget: int | None = None


class ReassessRequest(_SignalBase):
    """Recommend that a judgement or view be re-evaluated (e.g. stale)."""

    kind: Literal["reassess"] = "reassess"
    page_id: str


class PropagateFromChange(_SignalBase):
    """A change happened; recommend propagating its effects through dependents."""

    kind: Literal["propagate"] = "propagate"
    changed_page_id: str
    max_depth: int | None = None


class ConsolidateRequest(_SignalBase):
    """Recommend merging or deduplicating a set of pages."""

    kind: Literal["consolidate"] = "consolidate"
    page_ids: list[str]


class RobustifyRequest(_SignalBase):
    """Recommend generating variants of a claim."""

    kind: Literal["robustify"] = "robustify"
    claim_id: str


class ElicitInput(_SignalBase):
    """Recommend asking a human for input."""

    kind: Literal["elicit"] = "elicit"
    prompt: str
    question_id: str | None = None


FollowUp = (
    FocusRequest
    | ReassessRequest
    | PropagateFromChange
    | ConsolidateRequest
    | RobustifyRequest
    | ElicitInput
)
