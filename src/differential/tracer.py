"""Execution tracing: capture call events and persist them to the DB."""

import logging
from datetime import datetime, timezone

from differential.broadcast import Broadcaster
from differential.database import DB
from differential.trace_events import TraceEvent
from differential.settings import get_settings

log = logging.getLogger(__name__)


class CallTrace:
    """Accumulates trace events during a call and persists them to the DB."""

    def __init__(self, call_id: str, db: DB, broadcaster: Broadcaster | None = None):
        self.call_id = call_id
        self.db = db
        self.events: list[dict] = []
        self._enabled = get_settings().tracing_enabled
        self._broadcaster = broadcaster

    async def record(self, event_data: TraceEvent) -> None:
        if not self._enabled:
            return
        dumped = event_data.model_dump()
        dumped["ts"] = datetime.now(timezone.utc).isoformat()
        dumped["call_id"] = self.call_id
        self.events.append(dumped)
        if self._broadcaster:
            try:
                await self._broadcaster.send(dumped["event"], dumped)
            except Exception as e:
                log.debug("Broadcast failed for event %s: %s", dumped["event"], e)

    async def save(self) -> None:
        if not self._enabled:
            return
        if self.events:
            await self.db.save_call_trace(self.call_id, self.events)
            self.events = []
