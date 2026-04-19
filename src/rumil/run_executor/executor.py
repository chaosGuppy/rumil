"""RunExecutor: control-plane entry point for long-running runs.

Today the executor owns:

- ``status(run_id)`` — read the enriched ``RunView``
- ``mark_started/complete/failed/cancelled`` — direct status transitions
- ``create_run_from_spec`` + ``tracked_scope`` — additive scaffolding that
  dispatch paths can opt into without full migration
- ``start(spec)`` — full integrated path: creates the run row, looks up a
  handler for ``spec.kind``, spawns the handler as an asyncio.Task wrapped
  in ``tracked_scope``, and registers it in a process-global task table so
  cancel / wait can find it.
- ``cancel(run_id, reason)`` — marks cancelled and cancels the task if we
  own one
- ``wait_until_settled(run_id, timeout)`` — waits on the task (if known)
  and returns the final ``RunView``
- ``sum_call_costs(run_id)`` + ``would_exceed_budget(run_id)`` — dollar
  circuit-breaker helpers that callers can poll before dispatching more
  calls

Still pending:

- ``pause`` / ``resume`` — requires orchestrator cooperation (checking
  a paused flag between dispatches)
- ``run_checkpoints`` consumption — orchestrators write them today via
  no-op stubs; resume reads them in a follow-up phase.

``_ACTIVE_RUNS`` is module-level so that a ``RunExecutor(db_a)`` created
in the API layer can cancel / wait on a run started in the CLI layer by a
different ``RunExecutor(db_b)``. Each process owns its own copy; runs in
another worker aren't reachable (that's the cross-process layer).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from rumil.run_executor.run_spec import RunKind, RunSpec
from rumil.run_executor.run_state import RunEvent, RunStatus, RunView

if TYPE_CHECKING:
    from rumil.database import DB


log = logging.getLogger(__name__)


RunHandler = Callable[[RunSpec, "DB"], Awaitable[None]]


@dataclass
class _RunTask:
    """Process-local record of an in-flight run.

    ``task`` is the asyncio.Task running the handler inside
    ``tracked_scope``. ``cancel_reason`` is set by ``cancel()`` before
    ``task.cancel()`` so the tracked_scope exception handler can emit
    the right reason. ``cost_cap_usd_cents`` is the per-run dollar
    ceiling callers snapshot at ``start()`` time.
    """

    task: asyncio.Task[Any]
    cost_cap_usd_cents: int | None = None
    cancel_reason: str | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    event_queues: list[asyncio.Queue[RunEvent | None]] = field(default_factory=list)


_ACTIVE_RUNS: dict[str, _RunTask] = {}

EVENT_QUEUE_MAXSIZE = 256

_KIND_HANDLERS: dict[RunKind, RunHandler] = {}


def register_handler(kind: RunKind) -> Callable[[RunHandler], RunHandler]:
    """Register a coroutine as the handler for a RunSpec.kind.

    Handlers take ``(spec, db)`` and run the dispatch to completion. They
    are invoked inside ``RunExecutor.tracked_scope`` so they don't need
    to transition status themselves.
    """

    def deco(fn: RunHandler) -> RunHandler:
        if kind in _KIND_HANDLERS:
            raise ValueError(f"run handler already registered for kind={kind!r}")
        _KIND_HANDLERS[kind] = fn
        return fn

    return deco


def _get_handler(kind: RunKind) -> RunHandler:
    handler = _KIND_HANDLERS.get(kind)
    if handler is None:
        raise ValueError(
            f"No handler registered for RunSpec.kind={kind!r}. "
            f"Known: {sorted(_KIND_HANDLERS)}. "
            f"Import rumil.run_executor.handlers to install the defaults."
        )
    return handler


class _ExecutorCancelled(Exception):
    """Marker wrapped by ``tracked_scope`` so cancel → mark_cancelled."""


class RunExecutor:
    """Control-plane entry point for managing runs in this process."""

    def __init__(self, db: DB) -> None:
        self._db = db

    async def status(self, run_id: str) -> RunView | None:
        """Return the current RunView for ``run_id``, or None if absent."""
        row = await self._db.get_run(run_id)
        if row is None:
            return None
        created_at = _parse_ts(row.get("created_at")) or datetime.fromtimestamp(0)
        cost_cents = row.get("cost_usd_cents") or 0
        entry = _ACTIVE_RUNS.get(row["id"])
        in_flight = 1 if entry is not None and not entry.task.done() else 0
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
            in_flight_calls=in_flight,
        )

    async def mark_started(self, run_id: str) -> None:
        """Transition a run from pending to running and stamp started_at."""
        old = await self._read_status(run_id)
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
        await self._emit_status_changed(run_id, old)

    async def mark_complete(
        self,
        run_id: str,
        *,
        cost_usd_cents: int | None = None,
    ) -> None:
        old = await self._read_status(run_id)
        update: dict[str, Any] = {
            "status": RunStatus.COMPLETE.value,
            "finished_at": datetime.now(UTC).isoformat(),
        }
        if cost_usd_cents is not None:
            update["cost_usd_cents"] = cost_usd_cents
        await self._db._execute(self._db.client.table("runs").update(update).eq("id", run_id))
        await self._emit_status_changed(run_id, old)

    async def mark_failed(self, run_id: str, *, reason: str | None = None) -> None:
        old = await self._read_status(run_id)
        update: dict[str, Any] = {
            "status": RunStatus.FAILED.value,
            "finished_at": datetime.now(UTC).isoformat(),
        }
        if reason is not None:
            update["cancel_reason"] = reason
        await self._db._execute(self._db.client.table("runs").update(update).eq("id", run_id))
        await self._emit_status_changed(run_id, old)

    async def mark_cancelled(self, run_id: str, *, reason: str = "") -> None:
        old = await self._read_status(run_id)
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
        await self._emit_status_changed(run_id, old)

    async def create_run_from_spec(
        self,
        spec: RunSpec,
        *,
        orchestrator: str | None = None,
    ) -> str:
        """Create the runs row + init budget from a RunSpec.

        Returns ``self._db.run_id``. Status stays at ``pending`` until
        ``tracked_scope`` transitions it (or ``start()`` does the same
        via its wrapper task).
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
        """Context manager that marks a run started/complete/failed/cancelled.

        On enter: ``pending → running`` (idempotent — only transitions
        pending rows). On clean exit: ``running → complete``. On
        ``asyncio.CancelledError``: ``running → cancelled`` with the
        cancel_reason previously stashed by ``cancel()`` on the
        ``_ACTIVE_RUNS`` entry (or the exception message if no entry).
        On other exceptions: ``running → failed`` with the exception's
        type+message. In all three exception cases the exception
        propagates so callers can observe it.
        """
        await self.mark_started(run_id)
        try:
            yield
        except asyncio.CancelledError:
            entry = _ACTIVE_RUNS.get(run_id)
            reason = (entry.cancel_reason if entry is not None else None) or "cancelled"
            try:
                await self.mark_cancelled(run_id, reason=reason[:500])
            except Exception:
                log.exception("tracked_scope: mark_cancelled failed")
            raise
        except BaseException as exc:
            reason = f"{type(exc).__name__}: {exc}"[:500]
            try:
                await self.mark_failed(run_id, reason=reason)
            except Exception:
                log.exception("tracked_scope: mark_failed failed")
            raise
        else:
            try:
                await self.mark_complete(run_id)
            except Exception:
                log.exception("tracked_scope: mark_complete failed")

    async def start(self, spec: RunSpec) -> str:
        """Integrated path: create run + spawn handler + track lifecycle.

        Returns the run_id (always ``self._db.run_id``). The handler for
        ``spec.kind`` is looked up from the registry — callers must have
        imported ``rumil.run_executor.handlers`` (or registered their own)
        before calling. The handler runs as an asyncio.Task inside
        ``tracked_scope``; ``_ACTIVE_RUNS`` tracks the task so
        ``cancel()`` and ``wait_until_settled()`` can reach it.

        Idempotent only on the DB row, not on the task: if a run_id is
        already registered, raises ValueError to avoid leaking handlers.
        """
        handler = _get_handler(spec.kind)
        if self._db.run_id in _ACTIVE_RUNS:
            raise ValueError(
                f"RunExecutor.start: run_id {self._db.run_id[:8]} already has an "
                f"in-flight task — refusing to spawn a second handler"
            )
        await self.create_run_from_spec(spec)

        run_id = self._db.run_id
        db = self._db

        async def _wrapped() -> None:
            async with self.tracked_scope(run_id):
                await handler(spec, db)

        task = asyncio.create_task(_wrapped(), name=f"run-{run_id[:8]}")
        entry = _RunTask(
            task=task,
            cost_cap_usd_cents=_usd_to_cents(spec.budget_usd),
        )
        _ACTIVE_RUNS[run_id] = entry

        def _cleanup(_: asyncio.Task[Any]) -> None:
            settled = _ACTIVE_RUNS.pop(run_id, None)
            if settled is None:
                return
            for queue in settled.event_queues:
                try:
                    queue.put_nowait(None)
                except asyncio.QueueFull:
                    pass

        task.add_done_callback(_cleanup)
        return run_id

    async def cancel(self, run_id: str, *, reason: str = "") -> None:
        """Cancel an in-flight run.

        If we own a task for this run_id, stash the reason and
        ``task.cancel()``. The task's ``tracked_scope`` wrapper will
        catch ``CancelledError`` and mark the run as cancelled with the
        stashed reason.

        If we don't own a task (external run, already-settled run),
        fall back to directly marking the DB row cancelled — useful for
        cleaning up status when a worker crashed mid-run.
        """
        entry = _ACTIVE_RUNS.get(run_id)
        if entry is None:
            await self.mark_cancelled(run_id, reason=reason or "cancelled externally")
            return
        entry.cancel_reason = reason or "cancelled"
        entry.task.cancel()

    async def wait_until_settled(
        self,
        run_id: str,
        timeout: float | None = None,
    ) -> RunView | None:
        """Wait for an in-flight run to settle, then return its RunView.

        Uses ``asyncio.shield`` so cancelling the caller doesn't propagate
        into the handler. If ``timeout`` elapses, returns the current
        (still-running) view rather than raising. If no task is registered
        for this run_id (external run, already-settled), returns the
        current view immediately.
        """
        entry = _ACTIVE_RUNS.get(run_id)
        if entry is not None and not entry.task.done():
            try:
                await asyncio.wait_for(asyncio.shield(entry.task), timeout=timeout)
            except TimeoutError:
                pass
            except asyncio.CancelledError:
                pass
            except Exception:
                # Handler errors are already recorded via tracked_scope;
                # don't re-raise them here.
                pass
        return await self.status(run_id)

    async def pause(self, run_id: str) -> None:
        """Flip a running run into ``paused`` state.

        The transition is DB-level only: ``status = paused`` +
        ``paused_at = now()``. Orchestrators cooperate by polling
        ``is_paused(run_id)`` between dispatch batches and sleeping /
        waiting until it goes false. No orchestrator polls today, so
        pausing a live run just stamps the status — the handler keeps
        running. When we flip orchestrators to cooperative pausing,
        this is the enforcement point.

        Only transitions from ``running`` so double-pause and
        pause-on-finished are no-ops.
        """
        old = await self._read_status(run_id)
        await self._db._execute(
            self._db.client.table("runs")
            .update(
                {
                    "status": RunStatus.PAUSED.value,
                    "paused_at": datetime.now(UTC).isoformat(),
                }
            )
            .eq("id", run_id)
            .eq("status", RunStatus.RUNNING.value)
        )
        new = await self._read_status(run_id)
        if old != new and new == RunStatus.PAUSED:
            self._emit_event(
                run_id,
                RunEvent(run_id=run_id, event="paused", payload={}),
            )
        self._emit_status_changed_from(run_id, old, new)

    async def resume(self, run_id: str) -> None:
        """Flip a paused run back to ``running`` and clear ``paused_at``.

        Only transitions from ``paused``. No automatic task restart —
        the task kept running through the pause (see ``pause()`` docs).
        Cooperative orchestrators observe the paused → running edge
        on their next ``is_paused()`` poll and resume dispatching.
        """
        old = await self._read_status(run_id)
        await self._db._execute(
            self._db.client.table("runs")
            .update(
                {
                    "status": RunStatus.RUNNING.value,
                    "paused_at": None,
                }
            )
            .eq("id", run_id)
            .eq("status", RunStatus.PAUSED.value)
        )
        new = await self._read_status(run_id)
        if old != new and new == RunStatus.RUNNING:
            self._emit_event(
                run_id,
                RunEvent(run_id=run_id, event="resumed", payload={}),
            )
        self._emit_status_changed_from(run_id, old, new)

    async def is_paused(self, run_id: str) -> bool:
        """Return True when the run's DB status is ``paused``.

        Orchestrators poll this between dispatches and ``await
        asyncio.sleep(...)`` while it's True. Returns False for unknown
        run_ids (conservatively, to avoid blocking on phantom runs).
        """
        row = await self._db.get_run(run_id)
        if row is None:
            return False
        return row.get("status") == RunStatus.PAUSED.value

    async def wait_while_paused(
        self,
        run_id: str,
        *,
        poll_interval: float = 1.0,
        max_wait: float | None = None,
    ) -> None:
        """Sleep in a poll loop while the run is paused.

        Convenience for orchestrator handlers: call this between
        dispatch batches to implement cooperative pausing. Returns as
        soon as ``is_paused`` goes false or ``max_wait`` elapses (if
        set). ``poll_interval`` trades responsiveness for DB load —
        1s is a reasonable default for minute-scale dispatches.
        """
        start = asyncio.get_running_loop().time()
        while await self.is_paused(run_id):
            if max_wait is not None:
                elapsed = asyncio.get_running_loop().time() - start
                if elapsed >= max_wait:
                    return
            await asyncio.sleep(poll_interval)

    async def _read_status(self, run_id: str) -> RunStatus | None:
        """Read just the ``status`` column for ``run_id``, or None if absent."""
        row = await self._db.get_run(run_id)
        if row is None:
            return None
        raw = row.get("status")
        return RunStatus(raw) if raw else None

    def _emit_event(self, run_id: str, event: RunEvent) -> None:
        """Fan out an event to every active subscriber for ``run_id``.

        Uses ``put_nowait`` with ``QueueFull`` swallowed so a slow
        subscriber cannot stall a status transition. Other exceptions
        are not swallowed — they indicate bugs, not backpressure.
        """
        entry = _ACTIVE_RUNS.get(run_id)
        if entry is None:
            return
        for queue in entry.event_queues:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    async def _emit_status_changed(self, run_id: str, old: RunStatus | None) -> None:
        """Emit a ``status_changed`` event if the DB status actually moved.

        Called after a ``mark_*`` update. Re-reads the status to handle
        conditional updates (e.g. ``mark_started`` only fires pending→running).
        """
        new = await self._read_status(run_id)
        self._emit_status_changed_from(run_id, old, new)

    def _emit_status_changed_from(
        self,
        run_id: str,
        old: RunStatus | None,
        new: RunStatus | None,
    ) -> None:
        if new is None or old == new:
            return
        self._emit_event(
            run_id,
            RunEvent(
                run_id=run_id,
                event="status_changed",
                payload={
                    "old": old.value if old else None,
                    "new": new.value,
                },
            ),
        )

    async def events(self, run_id: str) -> AsyncIterator[RunEvent]:
        """Yield lifecycle events for ``run_id`` as they happen.

        Per-subscriber asyncio.Queue broker: each caller gets its own
        queue so a slow consumer doesn't starve others. Emissions use
        ``put_nowait`` with a drop-on-full policy — see ``_emit_event``
        — so status transitions never block on a stuck subscriber.

        For runs not in ``_ACTIVE_RUNS`` (unknown, or already-settled),
        emit one synthetic ``status_changed`` snapshotting the current
        DB status and return. Unknown runs (no row) return immediately
        with no events.

        Subscribers should ``async for`` the returned iterator. The
        iterator terminates cleanly when the run's task settles (a
        sentinel ``None`` is enqueued by the ``add_done_callback``).
        """
        entry = _ACTIVE_RUNS.get(run_id)
        if entry is None:
            view = await self.status(run_id)
            if view is None:
                return
            yield RunEvent(
                run_id=run_id,
                event="status_changed",
                payload={"old": None, "new": view.status.value},
            )
            return
        queue: asyncio.Queue[RunEvent | None] = asyncio.Queue(maxsize=EVENT_QUEUE_MAXSIZE)
        entry.event_queues.append(queue)
        try:
            while True:
                item = await queue.get()
                if item is None:
                    return
                yield item
        finally:
            try:
                entry.event_queues.remove(queue)
            except ValueError:
                pass

    async def checkpoint(
        self,
        run_id: str,
        kind: str,
        payload: dict[str, Any] | None = None,
    ) -> int:
        """Append a checkpoint row for a run and return its ``seq``.

        Orchestrators opt in by calling this at stage boundaries
        (``orchestrator_tick`` between dispatch batches,
        ``cost_committed`` after a BudgetGate.commit, etc.). The
        ``run_checkpoints`` table is append-only; ``seq`` is allocated
        server-side by looking up the current max for the run and
        incrementing. Calls race-safely enough for our single-writer
        model — concurrent writes from the same run_id would hit the
        ``(run_id, seq)`` primary key and the loser retries.
        """
        existing = _rows_sync(
            await self._db._execute(
                self._db.client.table("run_checkpoints")
                .select("seq")
                .eq("run_id", run_id)
                .order("seq", desc=True)
                .limit(1)
            )
        )
        seq = 0 if not existing else int(existing[0]["seq"]) + 1
        await self._db._execute(
            self._db.client.table("run_checkpoints").insert(
                {
                    "run_id": run_id,
                    "seq": seq,
                    "kind": kind,
                    "payload": payload or {},
                }
            )
        )
        self._emit_event(
            run_id,
            RunEvent(
                run_id=run_id,
                event="checkpointed",
                payload={"seq": seq, "kind": kind},
            ),
        )
        return seq

    async def latest_checkpoint(
        self,
        run_id: str,
        *,
        kind: str | None = None,
    ) -> dict[str, Any] | None:
        """Return the most recent checkpoint row for ``run_id``, or None.

        Optionally filter by ``kind`` (e.g. ``"orchestrator_tick"``).
        Returns the raw dict — caller reads ``payload`` as needed.
        """
        query = (
            self._db.client.table("run_checkpoints")
            .select("seq,kind,payload,created_at")
            .eq("run_id", run_id)
            .order("seq", desc=True)
            .limit(1)
        )
        if kind is not None:
            query = query.eq("kind", kind)
        rows = _rows_sync(await self._db._execute(query))
        return rows[0] if rows else None

    async def list_checkpoints(
        self,
        run_id: str,
        *,
        kind: str | None = None,
    ) -> list[dict[str, Any]]:
        """All checkpoints for ``run_id``, oldest first, optionally by kind."""
        query = (
            self._db.client.table("run_checkpoints")
            .select("seq,kind,payload,created_at")
            .eq("run_id", run_id)
            .order("seq")
        )
        if kind is not None:
            query = query.eq("kind", kind)
        return list(_rows_sync(await self._db._execute(query)))

    async def is_resumable(self, run_id: str) -> bool:
        """True when a run crashed (status=running but not in _ACTIVE_RUNS)
        and has at least one checkpoint to resume from.

        Orchestrator handlers can poll this at startup to decide whether
        to re-hydrate from checkpoints vs start fresh. No handler uses it
        yet — the surface is ready for when resumable orchestrators land.
        """
        if run_id in _ACTIVE_RUNS:
            return False
        view = await self.status(run_id)
        if view is None or view.status != RunStatus.RUNNING:
            return False
        latest = await self.latest_checkpoint(run_id)
        return latest is not None

    async def sum_call_costs(self, run_id: str) -> int:
        """Sum ``call_costs.usd`` for a run, in cents.

        Source of truth for the dollar circuit breaker. Issues one
        query. Zero-cost when a run has no calls yet.
        """
        result = await self._db._execute(
            self._db.client.table("call_costs").select("usd").eq("run_id", run_id)
        )
        total_usd = 0.0
        for row in result.data or []:
            try:
                total_usd += float(row.get("usd") or 0.0)
            except (TypeError, ValueError):
                continue
        return int(round(total_usd * 100))

    async def would_exceed_budget(self, run_id: str) -> bool:
        """Returns True when spend has reached (or exceeded) the run's cap.

        ``budget_usd`` is pinned per-run at ``start()`` time from the
        ``RunSpec`` and stashed on the ``_ACTIVE_RUNS`` entry. Runs
        started without a cap (or runs not tracked in this process)
        never trip — no false positives.
        """
        entry = _ACTIVE_RUNS.get(run_id)
        if entry is None or entry.cost_cap_usd_cents is None:
            return False
        spent = await self.sum_call_costs(run_id)
        return spent >= entry.cost_cap_usd_cents


def _rows_sync(result: Any) -> list[dict[str, Any]]:
    """Extract .data from a postgrest response as a list of dicts.

    Kept local to this module so the executor doesn't depend on
    rumil.db.row_helpers (which has a ``_rows`` helper with the same
    shape but is scoped to the stores).
    """
    data = getattr(result, "data", None)
    return list(data) if data else []


def _usd_to_cents(value: Decimal | None) -> int | None:
    if value is None:
        return None
    return int((value * Decimal(100)).to_integral_value())


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
