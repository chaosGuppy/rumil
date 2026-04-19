"""RunSpec: declarative description of what a run should do.

Passed to ``RunExecutor.start(spec)`` in future phases; the CLI,
API, parma, and chat skills all construct a RunSpec rather than
reimplementing the scaffold. Today this is data-only — no consumers
yet — so the shape is captured here ahead of the actual start()
implementation.

Kind-specific payload lives in ``payload`` (orchestrator args,
evaluation type, ingest source_page, etc.). Keeping it loose for now;
a stricter discriminated-union can come when the kinds stabilize.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

from rumil.models import CallType

RunKind = Literal[
    "orchestrator",
    "evaluation",
    "grounding_pipeline",
    "single_call",
    "ingest",
    "refine_artifact",
]


RunOrigin = Literal["cli", "api", "parma", "chat"]


@dataclass(frozen=True)
class RunSpec:
    """What a run is supposed to do + its caps.

    ``budget_calls`` (count pacing) maps onto the existing ``budget``
    table. ``budget_usd`` (dollar cap) is new and is what a future
    ``BudgetGate`` enforces via the ``call_costs`` table landed in
    migration ``20260419102100_run_executor_schema``.
    """

    kind: RunKind
    project_id: str
    question_id: str | None = None
    budget_calls: int | None = None
    budget_usd: Decimal | None = None
    max_inflight_calls: int = 4
    per_call_type_caps: dict[CallType, int] | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    staged: bool = False
    origin: RunOrigin = "cli"
    config_snapshot: dict[str, Any] = field(default_factory=dict)
    name: str = ""
    prompt_version: str | None = None
