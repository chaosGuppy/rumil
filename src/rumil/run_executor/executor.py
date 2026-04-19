"""RunExecutor: read-only today, full control plane in future phases.

Phase 2 exposes ``status(run_id)`` only. Everything else
(``start`` / ``pause`` / ``resume`` / ``cancel`` / ``wait_until_settled``
/ ``events``) is stubbed with NotImplementedError so callers can write
against the contract while the imperative dispatch path still owns
writes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from rumil.run_executor.run_spec import RunSpec
from rumil.run_executor.run_state import RunStatus, RunView

if TYPE_CHECKING:
    from rumil.database import DB


class RunExecutor:
    """Read-only façade over the ``runs`` table for now.

    A future phase turns this into a process-wide singleton that owns
    ``dict[run_id, _RunTask]``, a global max-concurrent-runs semaphore,
    and per-run ``InflightLimiter`` + ``BudgetGate``. Today the class
    is stateless; instances are cheap to construct.
    """

    def __init__(self, db: DB) -> None:
        self._db = db

    async def status(self, run_id: str) -> RunView | None:
        """Return the current RunView for ``run_id``, or None if absent.

        Reads the run row plus its config. Live counters
        (``in_flight_calls``, ``spent_usd_live``) default to zero /
        None; they come online when Phase 3 tracks in-process state.
        """
        row = await self._db.get_run(run_id)
        if row is None:
            return None
        created_at = _parse_ts(row.get("created_at")) or datetime.fromtimestamp(0)
        cost_cents = row.get("cost_usd_cents") or 0
        return RunView(
            run_id=row["id"],
            project_id=row.get("project_id") or "",
            question_id=row.get("question_id"),
            name=row.get("name") or "",
            status=RunStatus(row.get("status") or "pending"),
            created_at=created_at,
            started_at=_parse_ts(row.get("started_at")),
            finished_at=_parse_ts(row.get("finished_at")),
            cost_usd=Decimal(cost_cents) / Decimal(100),
            paused_at=_parse_ts(row.get("paused_at")),
            cancel_reason=row.get("cancel_reason"),
            staged=bool(row.get("staged", False)),
            hidden=bool(row.get("hidden", False)),
            config=row.get("config") or {},
        )

    async def start(self, spec: RunSpec) -> str:  # pragma: no cover
        raise NotImplementedError(
            "RunExecutor.start() lands in Phase 3 of the control-plane refactor. "
            "Until then construct runs via main.py / scripts/run_call.py / "
            "api/app.py as before."
        )

    async def pause(self, run_id: str) -> None:  # pragma: no cover
        raise NotImplementedError("RunExecutor.pause() lands in Phase 4.")

    async def resume(self, run_id: str) -> None:  # pragma: no cover
        raise NotImplementedError("RunExecutor.resume() lands in Phase 4.")

    async def cancel(self, run_id: str, *, reason: str = "") -> None:  # pragma: no cover
        raise NotImplementedError("RunExecutor.cancel() lands in Phase 4.")

    async def wait_until_settled(
        self, run_id: str, timeout: float | None = None
    ) -> RunView:  # pragma: no cover
        raise NotImplementedError("RunExecutor.wait_until_settled() lands in Phase 4.")

    def events(self, run_id: str) -> AsyncIterator[Any]:  # pragma: no cover
        raise NotImplementedError("RunExecutor.events() lands in Phase 4.")


def _parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None
