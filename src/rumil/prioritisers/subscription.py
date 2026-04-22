"""Subscription primitive for the prioritiser substrate.

A Subscription is a future that resolves when a target prioritiser's
``cumulative_spent`` reaches a given threshold. V1 uses this as the
synchronisation point between parent orchestrators and the shared
target prioritiser; V2 will expose ``subscribe`` and ``transfer`` as
first-class LLM-callable moves.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

log = logging.getLogger(__name__)


FireReason = Literal[
    "budget-spent",
    "already-satisfied",
    "teardown",
    "crashed",
    "marked-done",
    "cycle-skipped",
]


@dataclass
class Subscription:
    trigger_threshold: int
    future: asyncio.Future
    subscriber: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    fired_reason: FireReason | None = None
    target_question_id: str | None = None

    def is_ready(self, cumulative_spent: int) -> bool:
        return cumulative_spent >= self.trigger_threshold

    def resolve(
        self,
        delivered_call_id: str | None,
        reason: FireReason = "budget-spent",
    ) -> None:
        if self.future.done():
            return
        self.fired_reason = reason
        self.future.set_result(delivered_call_id)
        log.debug(
            "subscription fired: target=%s subscriber=%s threshold=%d "
            "reason=%s delivered_call_id=%s",
            (self.target_question_id or "?")[:8],
            (self.subscriber or "<root>")[:8],
            self.trigger_threshold,
            reason,
            delivered_call_id,
        )
