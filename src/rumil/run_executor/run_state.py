"""RunStatus enum + RunView projection."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Literal


class RunStatus(str, Enum):
    """Lifecycle state for a run.

    Matches the CHECK constraint on ``runs.status`` from the
    ``20260419102100_run_executor_schema`` migration. ``pending`` is the
    default for newly-created rows; the current imperative dispatch path
    does not yet transition to ``running``, so most non-cancelled runs
    stay at ``pending`` until Phase 3 wires start() / complete()
    transitions.
    """

    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class RunView:
    """Flattened ``runs`` row + live counters, as seen by the RunExecutor.

    Phase 2 populates only the DB-backed fields. Live counters
    (``in_flight_calls``, ``spent_usd_live``) are placeholders that come
    online when ``RunExecutor.start()`` begins tracking in-process state.
    """

    run_id: str
    project_id: str
    question_id: str | None
    name: str
    status: RunStatus
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    cost_usd: Decimal
    paused_at: datetime | None
    cancel_reason: str | None
    staged: bool
    hidden: bool
    config: dict

    in_flight_calls: int = 0
    spent_usd_live: Decimal | None = None


RunEventKind = Literal["status_changed", "checkpointed", "paused", "resumed"]


@dataclass(frozen=True)
class RunEvent:
    """A lifecycle event emitted by ``RunExecutor`` for a run.

    Broadcast over per-subscriber asyncio.Queues by
    ``RunExecutor.events(run_id)``. ``payload`` is kind-specific —
    ``status_changed`` carries ``{"old": <RunStatus>, "new": <RunStatus>}``;
    ``checkpointed`` carries ``{"seq": int, "kind": str}``; ``paused`` and
    ``resumed`` carry no additional fields by default.
    """

    run_id: str
    event: RunEventKind
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    payload: dict[str, Any] = field(default_factory=dict)
