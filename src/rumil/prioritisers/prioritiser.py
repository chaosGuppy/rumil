"""Per-question Prioritiser actor.

Each Prioritiser owns its local budget, subscription list, and cumulative
spend counter for one question under one registry. Concrete subclasses
implement ``_run_round`` (one prioritisation round) and
``_fire_subscription`` (how to deliver the per-subscriber result).

The round loop runs rounds while ``budget > 0``. When budget drains, the
loop exits naturally — the task terminates but the prioritiser *object*
stays in the registry. Subsequent ``receive_budget`` calls respawn a
fresh task via ``start()``. This avoids keeping an idle task per node
alive across the whole run, while still supporting Scenario B
collisions: parent2 can transfer budget after parent1's allocation has
been drained, and a fresh task picks up from the existing
``cumulative_spent`` / subscription state.

Explicit ``mark_done()`` is the only way to *permanently* close a
prioritiser. ``registry.teardown()`` calls it on all known prios at run
end so pending subscriptions get force-fired via the subclass hook and
any parent awaits unblock.

``run_body`` is kept for V1 facade compatibility — it runs the supplied
coroutine body and marks done on exit.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Literal

from rumil.prioritisers.subscription import Subscription

log = logging.getLogger(__name__)

PrioritiserState = Literal["idle", "running", "done"]


class Prioritiser:
    def __init__(self, question_id: str, kind: str = "question") -> None:
        self.question_id = question_id
        self.kind = kind
        self.budget: int = 0
        self.cumulative_spent: int = 0
        self.subscriptions: list[Subscription] = []
        self.done: asyncio.Event = asyncio.Event()
        self.state: PrioritiserState = "idle"
        self.crashed: bool = False
        self.crash_exc: BaseException | None = None
        self._lock: asyncio.Lock = asyncio.Lock()
        self._last_delivered_call_id: str | None = None
        self._task: asyncio.Task | None = None

    async def receive_budget(self, amount: int) -> None:
        async with self._lock:
            self.budget += amount
            already_done = self.state == "done"
        if already_done:
            return
        await self.start()

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
            target_question_id=self.question_id,
        )
        async with self._lock:
            if self.cumulative_spent >= threshold or self.state == "done":
                sub.resolve(
                    self._last_delivered_call_id,
                    reason="already-satisfied" if self.state != "done" else "marked-done",
                )
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
                sub.resolve(self._last_delivered_call_id, reason="budget-spent")
            else:
                still_pending.append(sub)
        self.subscriptions = still_pending

    async def forfeit_remaining_budget(self) -> None:
        """Close out leftover budget without spending it, preserving conservation.

        Why: subscriptions are created at ``threshold = pre_cumulative + budget_granted``
        on the assumption that the grant will eventually be spent. A raw
        ``self.budget = 0`` (e.g. on last_call exit) breaks that invariant —
        budget disappears without ``cumulative_spent`` rising, so subscribers
        block forever. Forfeiting charges the lost budget to cumulative_spent
        so pending subs whose thresholds fall inside the granted amount fire
        normally.
        """
        async with self._lock:
            forfeit = self.budget
            if forfeit <= 0:
                return
            self.budget = 0
            self.cumulative_spent += forfeit
            self._fire_ready_locked()

    async def mark_done(
        self,
        delivered_call_id: str | None = None,
        reason: str = "marked-done",
    ) -> None:
        async with self._lock:
            if self.state == "done":
                return
            if delivered_call_id is not None:
                self._last_delivered_call_id = delivered_call_id
            if reason == "crashed":
                fire_reason = "crashed"
            elif reason == "teardown":
                fire_reason = "teardown"
            else:
                fire_reason = "marked-done"
            for sub in self.subscriptions:
                sub.resolve(self._last_delivered_call_id, reason=fire_reason)
            self.subscriptions = []
            self.state = "done"
            self.done.set()

    async def await_completion(self) -> None:
        await self.done.wait()

    async def run_body(
        self,
        body: Callable[[], Awaitable[None]],
    ) -> None:
        """Execute an orchestrator body directly, mark done on exit.

        Kept for V1 facade compatibility. V2 callers should use the
        actor round loop via ``start()`` instead.
        """
        try:
            await body()
        finally:
            await self.mark_done()

    async def _run_round(self, round_budget: int) -> None:
        """Run one prioritisation round. Subclass hook.

        Invoked outside the claim lock so implementations are free to
        perform LLM calls and dispatches. Budget accounting is the
        subclass's responsibility (via ``on_dispatch_completed``).
        """
        raise NotImplementedError

    async def _fire_subscription(self, subscription: Subscription) -> None:
        """Produce a deliverable for a subscription and resolve its future.

        Subclass hook. Default implementation resolves the subscription
        with the most-recent delivered call id (``None`` if there's never
        been one). Subclasses may override to force a fresh view/assess
        call when a subscription fires with no recorded deliverable.
        """
        subscription.resolve(self._last_delivered_call_id)

    async def start(self) -> None:
        """Spawn the round-loop task if one isn't already running.

        Idempotent: repeated calls while a task is running are no-ops.
        Callers should invoke this after ``receive_budget`` or
        ``subscribe`` when they want the actor loop to process the new
        state — ``receive_budget`` does this automatically.
        """
        async with self._lock:
            if self.state == "done":
                return
            if self._task is not None and not self._task.done():
                return
            self._task = asyncio.create_task(
                self._round_loop(),
                name=f"prio:{self.question_id[:8]}",
            )

    async def _round_loop(self) -> None:
        """The actor round loop.

        Runs rounds while there's budget. Exits naturally when budget
        drains, leaving the prioritiser in ``idle`` state and its
        ``subscriptions`` list intact. A subsequent ``receive_budget``
        will respawn this loop via ``start()`` — so the prioritiser
        *object* persists in the registry but the *task* only exists
        while work is being done.

        Only ``mark_done()`` transitions the prioritiser to the terminal
        ``done`` state; teardown is the usual caller.
        """
        while True:
            async with self._lock:
                self._fire_ready_locked()
                if self.state == "done":
                    return
                if self.budget <= 0:
                    self.state = "idle"
                    return
                self.state = "running"
                round_budget = self.budget
            try:
                await self._run_round(round_budget)
            except Exception as exc:
                log.error(
                    "Prioritiser CRASHED: %s(%s) kind=%s budget=%d spent=%d pending_subs=%d exc=%r",
                    type(self).__name__,
                    self.question_id[:8],
                    self.kind,
                    self.budget,
                    self.cumulative_spent,
                    len(self.subscriptions),
                    exc,
                    exc_info=True,
                )
                self.crashed = True
                self.crash_exc = exc
                await self.mark_done(reason="crashed")
                return
            async with self._lock:
                self._fire_ready_locked()
                if self.state != "done":
                    self.state = "idle"
