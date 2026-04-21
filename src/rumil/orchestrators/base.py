"""
BaseOrchestrator: abstract base class for all orchestrators.
"""

import logging
from abc import ABC, abstractmethod

from rumil.database import DB
from rumil.orchestrators.common import (
    _create_broadcaster,
)
from rumil.prioritisers.dispatch import DispatchRunner
from rumil.tracing.broadcast import Broadcaster

log = logging.getLogger(__name__)


class BaseOrchestrator(DispatchRunner, ABC):
    summarise_before_assess: bool = True

    def __init__(self, db: DB, broadcaster: Broadcaster | None = None):
        self.db = db
        self.broadcaster: Broadcaster | None = broadcaster
        self._owns_broadcaster: bool = False
        self.ingest_hint: str = ""

    async def _setup(self) -> None:
        if not self.broadcaster:
            self.broadcaster = _create_broadcaster(self.db)
            self._owns_broadcaster = True
        log.info("Orchestrator: run_id=%s", self.db.run_id)
        total, used = await self.db.get_budget()
        log.info(
            "Orchestrator.run starting: budget=%d (used=%d)",
            total,
            used,
        )

    async def _teardown(self) -> None:
        if self.broadcaster and self._owns_broadcaster:
            await self.broadcaster.close()
        total, used = await self.db.get_budget()
        log.info("Orchestrator.run complete: budget used %d/%d", used, total)

    @abstractmethod
    async def run(self, root_question_id: str) -> None: ...
