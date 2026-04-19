"""RunExecutor: read-only today, full control plane in future phases.

Phase 2 exposes ``status(run_id)`` only. Everything else
(``start`` / ``pause`` / ``resume`` / ``cancel`` / ``wait_until_settled``
/ ``events``) is stubbed with NotImplementedError so callers can write
against the contract while the imperative dispatch path still owns
writes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
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

    async def mark_started(self, run_id: str) -> None:
        """Transition a run from pending to running and stamp started_at.

        Opt-in for dispatch paths (main.py cmd_*, scripts/run_call.py,
        api/app.py _run_background*) that want their runs to show up
        live in the status-aware UI while the full executor.start()
        refactor is still pending. Safe to call more than once — the
        update only fires when status is still ``pending``.
        """
        await self._db._execute(
            self._db.client.table("runs")
            .update(
                {
                    "status": RunStatus.RUNNING.value,
                    "started_at": datetime.now(UTC).isoformat(),
                }
            )
            .eq("id", run_id)
            .eq("status", RunStatus.PENDING.value)
        )

    async def mark_complete(
        self,
        run_id: str,
        *,
        cost_usd_cents: int | None = None,
    ) -> None:
        """Transition a run to complete + stamp finished_at (+ optional cost)."""
        update: dict[str, Any] = {
            "status": RunStatus.COMPLETE.value,
            "finished_at": datetime.now(UTC).isoformat(),
        }
        if cost_usd_cents is not None:
            update["cost_usd_cents"] = cost_usd_cents
        await self._db._execute(self._db.client.table("runs").update(update).eq("id", run_id))

    async def mark_failed(self, run_id: str, *, reason: str | None = None) -> None:
        """Transition a run to failed + stamp finished_at."""
        update: dict[str, Any] = {
            "status": RunStatus.FAILED.value,
            "finished_at": datetime.now(UTC).isoformat(),
        }
        if reason is not None:
            update["cancel_reason"] = reason
        await self._db._execute(self._db.client.table("runs").update(update).eq("id", run_id))

    async def mark_cancelled(self, run_id: str, *, reason: str = "") -> None:
        """Transition a run to cancelled + stamp finished_at + cancel_reason."""
        await self._db._execute(
            self._db.client.table("runs")
            .update(
                {
                    "status": RunStatus.CANCELLED.value,
                    "finished_at": datetime.now(UTC).isoformat(),
                    "cancel_reason": reason or None,
                }
            )
            .eq("id", run_id)
        )

    async def create_run_from_spec(
        self,
        spec: RunSpec,
        *,
        orchestrator: str | None = None,
    ) -> str:
        """Create the runs row + init budget from a RunSpec.

        Returns the run_id (always ``self._db.run_id``). Intended as the
        unified replacement for main.py's six cmd_* scaffolds and
        scripts/run_call.py's manual init. Callers then dispatch via
        their existing handler (dispatch_orchestrator / run_call / etc.)
        inside ``tracked_scope(run_id)`` for lifecycle tracking.

        Status stays at ``pending`` until ``tracked_scope`` transitions
        it. That lets the DB row exist for inspection + broadcasting
        before the actual dispatch kicks off.
        """
        if spec.staged and not self._db.staged:
            raise ValueError(
                "RunExecutor.create_run_from_spec: spec.staged=True requires "
                "the DB handle to have been created with staged=True"
            )
        config = {
            **spec.config_snapshot,
            "origin": spec.origin,
        }
        if spec.prompt_version is not None:
            config["pinned_prompt_version"] = spec.prompt_version
        await self._db.create_run(
            name=spec.name or f"{spec.kind}-{self._db.run_id[:8]}",
            question_id=spec.question_id,
            config=config,
            orchestrator=orchestrator,
        )
        if spec.budget_calls is not None and spec.budget_calls > 0:
            await self._db.init_budget(spec.budget_calls)
        return self._db.run_id

    @asynccontextmanager
    async def tracked_scope(self, run_id: str) -> AsyncIterator[None]:
        """Context manager that marks a run started/complete/failed.

        Usage::

            async with executor.tracked_scope(db.run_id):
                await dispatch_orchestrator(...)

        On enter: ``pending → running`` + stamps ``started_at``.
        On clean exit: ``running → complete`` + stamps ``finished_at``.
        On exception: ``running → failed`` with the exception's type+message
        as ``cancel_reason``, then re-raises.

        Idempotent-safe: ``mark_started`` only transitions pending rows, so
        wrapping a run twice won't stomp a second ``started_at``. The
        complete/failed branch unconditionally sets ``finished_at`` — the
        last scope to exit wins, which matches how async callers nest.
        """
        await self.mark_started(run_id)
        try:
            yield
        except BaseException as exc:
            reason = f"{type(exc).__name__}: {exc}"[:500]
            try:
                await self.mark_failed(run_id, reason=reason)
            except Exception:
                pass
            raise
        else:
            try:
                await self.mark_complete(run_id)
            except Exception:
                pass

    async def start(self, spec: RunSpec) -> str:  # pragma: no cover
        raise NotImplementedError(
            "RunExecutor.start() (fully managed: create + track + dispatch) "
            "lands in a later phase. Today callers invoke "
            "create_run_from_spec + tracked_scope + their own dispatcher."
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
