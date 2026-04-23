"""BudgetEnvelope: per-dimension caps a process is asked to respect.

Each field is optional. A process may consume only the dimensions it
cares about; dimensions left as None are treated as unbounded (subject
to project-level policy elsewhere).

In v1 only ``compute`` is actually enforced end-to-end — it maps onto
the existing per-run budget consumed by call dispatches. The remaining
fields are declared now so resource usage can be tracked uniformly and
so future scheduling policies (wall-clock caps, write quotas, human-
attention budgets) have a place to attach without reshaping the API.
"""

from pydantic import BaseModel, Field


class BudgetEnvelope(BaseModel):
    compute: int | None = Field(
        default=None,
        description="Dispatch units (maps to existing per-run budget)",
    )
    ws_reads: int | None = Field(
        default=None,
        description="Workspace read operations (page fetches, embedding queries)",
    )
    web: int | None = Field(
        default=None,
        description="External web fetches",
    )
    writes: int | None = Field(
        default=None,
        description="Pages and links created/superseded",
    )
    wallclock_seconds: float | None = None
    human_attention: int | None = Field(
        default=None,
        description="Turns of human interaction available",
    )


class ResourceUsage(BaseModel):
    """Actual consumption over the course of a process run."""

    compute: int = 0
    ws_reads: int = 0
    web: int = 0
    writes: int = 0
    wallclock_seconds: float = 0.0
    human_attention: int = 0
