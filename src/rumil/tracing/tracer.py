"""Execution tracing: capture call events and persist them to the DB."""

import contextvars
import logging
from datetime import UTC, datetime

from rumil.database import DB
from rumil.settings import get_settings
from rumil.tracing.broadcast import Broadcaster
from rumil.tracing.trace_events import LLMExchangeEvent, TraceEvent

log = logging.getLogger(__name__)

_trace_var: contextvars.ContextVar["CallTrace | None"] = contextvars.ContextVar(
    "rumil_call_trace", default=None
)


class TraceRecordError(RuntimeError):
    """Raised when a trace event could not be persisted.

    Caught by the caller of ``CallTrace.record_strict()``. The caller is
    typically a post-mutation site where DB state has already been updated
    and a missing trace event would leave the frontend's view of the call
    silently inconsistent with reality. The default ``CallTrace.record()``
    method logs and swallows failures instead, for mid-call diagnostic
    events where continuing is preferable to crashing the call.
    """


def get_trace() -> "CallTrace | None":
    """Return the current task-local CallTrace, or None if not set."""
    return _trace_var.get()


def set_trace(trace: "CallTrace") -> contextvars.Token:
    """Set the task-local CallTrace. Returns a token for optional reset."""
    return _trace_var.set(trace)


def reset_trace(token: contextvars.Token) -> None:
    """Restore the task-local CallTrace to its value before ``set_trace``."""
    _trace_var.reset(token)


class CallTrace:
    """Records trace events: persists each to the DB then broadcasts."""

    def __init__(self, call_id: str, db: DB, broadcaster: Broadcaster | None = None):
        self.call_id = call_id
        self.db = db
        self._enabled = get_settings().tracing_enabled
        self._broadcaster = broadcaster
        self.total_cost_usd: float = 0.0
        self._page_loads: list[dict] = []

    @property
    def broadcaster(self) -> Broadcaster | None:
        """Public accessor for the broadcaster (used by moves that spawn sub-calls)."""
        return self._broadcaster

    def record_page_load(self, page_id: str, detail: str, tags: dict[str, str]) -> None:
        """Accumulate a page-load event (flushed to DB at end of call)."""
        self._page_loads.append(
            {
                "page_id": page_id,
                "detail": detail,
                "tags": tags,
            }
        )

    async def flush_page_loads(self) -> None:
        """Batch-insert accumulated page-load events into the DB."""
        if not self._page_loads:
            return
        try:
            await self.db.save_page_format_events(self.call_id, self._page_loads)
        except Exception as e:
            log.error(
                "Failed to flush %d page-load events for call %s: %s",
                len(self._page_loads),
                self.call_id[:8],
                e,
            )
        self._page_loads.clear()

    def _prepare_event(self, event_data: TraceEvent) -> dict:
        if isinstance(event_data, LLMExchangeEvent) and event_data.cost_usd:
            self.total_cost_usd += event_data.cost_usd
        dumped = event_data.model_dump()
        dumped["ts"] = datetime.now(UTC).isoformat()
        dumped["call_id"] = self.call_id
        return dumped

    async def _broadcast(self, dumped: dict) -> None:
        if not self._broadcaster:
            return
        try:
            await self._broadcaster.send(dumped["event"], dumped)
        except Exception as e:
            log.warning("Broadcast failed for event %s: %s", dumped["event"], e)

    async def record(self, event_data: TraceEvent) -> None:
        """Record a trace event. Logs and continues on DB write failure.

        Use this for mid-call diagnostic events (LLM exchanges, context-built,
        warnings, errors during the call) where a silently-dropped event is
        annoying but not a correctness hazard. For post-mutation events
        whose absence would leave the frontend out of sync with DB state,
        use ``record_strict()``.
        """
        if not self._enabled:
            return
        dumped = self._prepare_event(event_data)
        try:
            await self.db.save_call_trace(self.call_id, [dumped])
        except Exception as e:
            log.error(
                "Failed to persist trace event %s for call %s: %s",
                dumped.get("event"),
                self.call_id[:8],
                e,
            )
        await self._broadcast(dumped)

    async def record_strict(self, event_data: TraceEvent) -> None:
        """Record a trace event, raising ``TraceRecordError`` on DB failure.

        Use this right after a workspace mutation has landed — e.g.
        persisted page creation, completed closing review, executed dispatch,
        saved subquestion links. Failing loud here is essential because the
        mutation is already live but the trace envelope would silently omit
        the event, leaving the frontend view of the call inconsistent with
        actual DB state.

        Broadcast failures are still logged-and-swallowed: the broadcast is
        only a live-update convenience, not a source of truth.
        """
        if not self._enabled:
            return
        dumped = self._prepare_event(event_data)
        try:
            await self.db.save_call_trace(self.call_id, [dumped])
        except Exception as e:
            log.error(
                "Failed to persist trace event %s for call %s: %s",
                dumped.get("event"),
                self.call_id[:8],
                e,
            )
            raise TraceRecordError(
                f"failed to persist {dumped.get('event')!r} event for call {self.call_id}: {e}"
            ) from e
        await self._broadcast(dumped)
