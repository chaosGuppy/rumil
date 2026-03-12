"""Execution tracing: capture call events and persist them to the DB."""

import logging
from datetime import datetime, timezone

from differential.tracing.broadcast import Broadcaster
from differential.database import DB
from differential.tracing.trace_events import TraceEvent
from differential.settings import get_settings

log = logging.getLogger(__name__)


class CallTrace:
    """Records trace events: persists each to the DB then broadcasts."""

    def __init__(self, call_id: str, db: DB, broadcaster: Broadcaster | None = None):
        self.call_id = call_id
        self.db = db
        self._enabled = get_settings().tracing_enabled
        self._broadcaster = broadcaster

    async def record(self, event_data: TraceEvent) -> None:
        if not self._enabled:
            return
        dumped = event_data.model_dump()
        dumped["ts"] = datetime.now(timezone.utc).isoformat()
        dumped["call_id"] = self.call_id
        await self.db.save_call_trace(self.call_id, [dumped])
        if self._broadcaster:
            try:
                await self._broadcaster.send(dumped["event"], dumped)
            except Exception as e:
                log.debug("Broadcast failed for event %s: %s", dumped["event"], e)
