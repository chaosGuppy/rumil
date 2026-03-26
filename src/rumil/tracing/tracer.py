"""Execution tracing: capture call events and persist them to the DB."""

import contextvars
import logging
from datetime import datetime, timezone

from rumil.tracing.broadcast import Broadcaster
from rumil.database import DB
from rumil.tracing.trace_events import LLMExchangeEvent, TraceEvent
from rumil.settings import get_settings

log = logging.getLogger(__name__)

_trace_var: contextvars.ContextVar["CallTrace | None"] = contextvars.ContextVar(
    "rumil_call_trace", default=None
)


def get_trace() -> "CallTrace | None":
    """Return the current task-local CallTrace, or None if not set."""
    return _trace_var.get()


def set_trace(trace: "CallTrace") -> contextvars.Token:
    """Set the task-local CallTrace. Returns a token for optional reset."""
    return _trace_var.set(trace)


class CallTrace:
    """Records trace events: persists each to the DB then broadcasts."""

    def __init__(self, call_id: str, db: DB, broadcaster: Broadcaster | None = None):
        self.call_id = call_id
        self.db = db
        self._enabled = get_settings().tracing_enabled
        self._broadcaster = broadcaster
        self.total_cost_usd: float = 0.0

    async def record(self, event_data: TraceEvent) -> None:
        if not self._enabled:
            return
        if isinstance(event_data, LLMExchangeEvent) and event_data.cost_usd:
            self.total_cost_usd += event_data.cost_usd
        dumped = event_data.model_dump()
        dumped["ts"] = datetime.now(timezone.utc).isoformat()
        dumped["call_id"] = self.call_id
        try:
            await self.db.save_call_trace(self.call_id, [dumped])
        except Exception as e:
            log.error(
                "Failed to persist trace event %s for call %s: %s",
                dumped.get("event"),
                self.call_id[:8],
                e,
            )
        if self._broadcaster:
            try:
                await self._broadcaster.send(dumped["event"], dumped)
            except Exception as e:
                log.warning("Broadcast failed for event %s: %s", dumped["event"], e)
