"""Per-question Prioritiser (V1 skeleton).

V1 shape: each Prioritiser represents the completion of research work on
one question under one registry. It exposes a completion ``asyncio.Event``
plus budget/cumulative-spent counters and a subscription list so V2 can
add the round loop and transfer+subscribe semantics without re-shaping
callers.

The ``run_body`` callable is the orchestrator-specific work (e.g. the
TwoPhase round loop). V1 runs it inline under the claim lock; V2 will
dispatch it to an ``asyncio.Task`` owned by the Prioritiser.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from rumil.prioritisers.subscription import Subscription

log = logging.getLogger(__name__)


class Prioritiser:
    def __init__(self, question_id: str, kind: str = "question") -> None:
        self.question_id = question_id
        self.kind = kind
        self.budget: int = 0
        self.cumulative_spent: int = 0
        self.subscriptions: list[Subscription] = []
        self.done: asyncio.Event = asyncio.Event()
        self._lock: asyncio.Lock = asyncio.Lock()
        self._last_delivered_call_id: str | None = None

    async def receive_budget(self, amount: int) -> None:
        async with self._lock:
            self.budget += amount

    async def subscribe(
        self,
        threshold: int,
        subscriber: str | None = None,
    ) -> asyncio.Future:
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        sub = Subscription(
            trigger_threshold=threshold,
            future=future,
            subscriber=subscriber,
        )
        async with self._lock:
            if self.cumulative_spent >= threshold:
                sub.resolve(self._last_delivered_call_id)
            else:
                self.subscriptions.append(sub)
        return future

    async def on_dispatch_completed(
        self,
        cost: int,
        delivered_call_id: str | None = None,
    ) -> None:
        async with self._lock:
            self.cumulative_spent += cost
            self.budget = max(0, self.budget - cost)
            if delivered_call_id is not None:
                self._last_delivered_call_id = delivered_call_id
            self._fire_ready_locked()

    def _fire_ready_locked(self) -> None:
        still_pending: list[Subscription] = []
        for sub in self.subscriptions:
            if sub.is_ready(self.cumulative_spent):
                sub.resolve(self._last_delivered_call_id)
            else:
                still_pending.append(sub)
        self.subscriptions = still_pending

    async def mark_done(self, delivered_call_id: str | None = None) -> None:
        async with self._lock:
            if delivered_call_id is not None:
                self._last_delivered_call_id = delivered_call_id
            for sub in self.subscriptions:
                sub.resolve(self._last_delivered_call_id)
            self.subscriptions = []
            self.done.set()

    async def await_completion(self) -> None:
        await self.done.wait()

    async def run_body(
        self,
        body: Callable[[], Awaitable[None]],
    ) -> None:
        """Execute an orchestrator body under the claim lock, mark done on exit."""
        try:
            await body()
        finally:
            await self.mark_done()
