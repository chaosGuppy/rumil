"""Subscription primitive for the prioritiser substrate.

A Subscription is a future that resolves when a target prioritiser's
``cumulative_spent`` reaches a given threshold. V1 uses this as the
synchronisation point between parent orchestrators and the shared
target prioritiser; V2 will expose ``subscribe`` and ``transfer`` as
first-class LLM-callable moves.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class Subscription:
    trigger_threshold: int
    future: asyncio.Future
    subscriber: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def is_ready(self, cumulative_spent: int) -> bool:
        return cumulative_spent >= self.trigger_threshold

    def resolve(self, delivered_call_id: str | None) -> None:
        if not self.future.done():
            self.future.set_result(delivered_call_id)
