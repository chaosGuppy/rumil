"""PrioritiserRegistry: per-DB dedup of prioritisation work.

Shared across all forks of a root DB. The registry supports:

* ``get_or_acquire(question_id)`` — returns ``(Prioritiser, is_new)``.
  ``is_new=True`` means the caller owns the prioritiser and should run
  its body. ``is_new=False`` means a Prioritiser already exists and the
  caller should ``await prio.await_completion()`` or subscribe.
* ``should_execute_non_scope_dispatch(target_q, call_type)`` — returns
  ``True`` on first call for a given ``(target_q, call_type)`` pair and
  ``False`` on subsequent calls. Used to dedup cross-parent dispatches
  on a shared child.
* ``teardown()`` — fires any still-pending subscriptions so parent
  awaits don't hang at run end.
"""

import asyncio
import logging
import weakref
from typing import TYPE_CHECKING

from rumil.models import CallType
from rumil.prioritisers.prioritiser import Prioritiser
from rumil.tracing.trace_events import (
    BudgetTransferredEvent,
    SubscriptionCreatedEvent,
    SubscriptionFiredEvent,
)

if TYPE_CHECKING:
    from rumil.database import DB
    from rumil.tracing.broadcast import Broadcaster

log = logging.getLogger(__name__)


_ALL_REGISTRIES: "weakref.WeakSet[PrioritiserRegistry]" = weakref.WeakSet()


def all_registries() -> list["PrioritiserRegistry"]:
    """Snapshot of live PrioritiserRegistry instances. For diagnostics only."""
    return list(_ALL_REGISTRIES)


class PrioritiserRegistry:
    def __init__(self) -> None:
        self._by_question: dict[str, Prioritiser] = {}
        self._non_scope_dispatched: set[tuple[str, str]] = set()
        self._lock: asyncio.Lock = asyncio.Lock()
        self._pending_trace_tasks: set[asyncio.Task] = set()
        _ALL_REGISTRIES.add(self)

    async def get_or_acquire(
        self,
        question_id: str,
        kind: str = "question",
        factory: type[Prioritiser] | None = None,
    ) -> tuple[Prioritiser, bool]:
        async with self._lock:
            existing = self._by_question.get(question_id)
            if existing is not None:
                return existing, False
            cls = factory or Prioritiser
            prio = cls(question_id, kind=kind)
            self._by_question[question_id] = prio
            return prio, True

    async def get(self, question_id: str) -> Prioritiser | None:
        async with self._lock:
            return self._by_question.get(question_id)

    def _would_create_subscription_cycle(
        self,
        subscriber_question_id: str | None,
        target_question_id: str,
    ) -> bool:
        """Does adding subscriber→target create a cycle in the subscription graph?

        An edge X→Y means X is awaiting Y (X has a pending subscription
        *on* Y, recorded in Y.subscriptions with sub.subscriber=X).
        A cycle appears when we add subscriber→target and there is
        already a path target→…→subscriber, because both tasks then
        block forever on each other inside ``asyncio.gather``.

        We DFS from ``target`` along outgoing edges; outgoing from a
        node X is the set of Y whose ``subscriptions`` list contains a
        sub with ``subscriber == X``.

        Caller must hold ``self._lock``.
        """
        if subscriber_question_id is None:
            return False
        if subscriber_question_id == target_question_id:
            return True
        seen: set[str] = set()
        stack: list[str] = [target_question_id]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            for y_qid, y_prio in self._by_question.items():
                if y_qid in seen:
                    continue
                if any(sub.subscriber == current for sub in y_prio.subscriptions):
                    if y_qid == subscriber_question_id:
                        return True
                    stack.append(y_qid)
        return False

    async def should_execute_non_scope_dispatch(
        self,
        target_question_id: str,
        call_type: CallType,
    ) -> bool:
        """Claim a non-scope dispatch slot. Returns ``True`` if first, ``False`` if duplicate."""
        key = (target_question_id, call_type.value)
        async with self._lock:
            if key in self._non_scope_dispatched:
                return False
            self._non_scope_dispatched.add(key)
            return True

    async def recurse(
        self,
        target_question_id: str,
        budget: int,
        *,
        factory: type[Prioritiser] | None = None,
        kind: str = "question",
        db: "DB | None" = None,
        broadcaster: "Broadcaster | None" = None,
        subscriber: str | None = None,
    ) -> asyncio.Future:
        """Transfer+subscribe primitive: grow target's budget, subscribe at new high-water.

        Gets (or creates) the target prioritiser via ``factory``. If just
        created, attaches ``db``/``broadcaster`` so its
        ``_fire_subscription`` hook can produce a real deliverable.
        Transfers ``budget`` and subscribes at
        ``target.cumulative_spent + target.budget`` — i.e. once the newly
        contributed budget has been spent.

        Returns a future that resolves with the delivered call id when
        the threshold is met (or ``registry.teardown`` force-fires).
        """
        prio, is_new = await self.get_or_acquire(
            target_question_id,
            kind=kind,
            factory=factory,
        )
        if is_new and db is not None and hasattr(prio, "attach"):
            prio.attach(db, broadcaster)  # type: ignore[attr-defined]
        from rumil.tracing.tracer import get_trace

        trace = get_trace()
        if trace is not None:
            await trace.record(
                BudgetTransferredEvent(
                    from_question_id=subscriber,
                    to_question_id=target_question_id,
                    amount=budget,
                )
            )
        # Cycle detection: if subscribing here would create a cycle in
        # the subscription graph (A→B plus an existing B→...→A chain),
        # both round tasks would block on each other's subscription
        # future inside asyncio.gather and deadlock. Transfer the budget
        # but skip the subscription; return a pre-resolved future so the
        # parent's gather proceeds.
        async with self._lock:
            cycle = self._would_create_subscription_cycle(subscriber, target_question_id)
        # Snapshot cumulative_spent and grow budget atomically so the
        # threshold = pre_cumulative + budget represents "our contributed
        # B has been spent", even if a round overspends (which clamps
        # budget to 0 and would otherwise drop cumulative+budget below
        # the real spend-level by the time we compute it).
        async with prio._lock:
            pre_cumulative = prio.cumulative_spent
            prio.budget += budget
            already_done = prio.state == "done"
            threshold = pre_cumulative + budget
        if not already_done:
            await prio.start()
        if cycle:
            log.info(
                "Registry.recurse: skipping subscription from %s to %s to avoid cycle",
                (subscriber or "<root>")[:8],
                target_question_id[:8],
            )
            loop = asyncio.get_running_loop()
            pre_resolved: asyncio.Future = loop.create_future()
            pre_resolved.set_result(None)
            return pre_resolved
        if trace is not None:
            await trace.record(
                SubscriptionCreatedEvent(
                    target_question_id=target_question_id,
                    trigger_threshold=threshold,
                    subscriber=subscriber,
                )
            )
        future = await prio.subscribe(threshold=threshold, subscriber=subscriber)
        if trace is not None:

            def _on_resolve(fut: asyncio.Future) -> None:
                if fut.cancelled() or fut.exception() is not None:
                    return
                delivered_call_id = fut.result()
                task = asyncio.create_task(
                    trace.record(
                        SubscriptionFiredEvent(
                            target_question_id=target_question_id,
                            delivered_call_id=delivered_call_id,
                        )
                    )
                )
                self._pending_trace_tasks.add(task)
                task.add_done_callback(self._pending_trace_tasks.discard)

            future.add_done_callback(_on_resolve)
        if is_new:
            await prio.start()
        return future

    async def teardown(self) -> None:
        """Resolve any still-pending prioritisers so parents don't hang.

        Pending subscriptions on each prio get fired via the prio's
        ``_fire_subscription`` hook (subclasses may produce a real
        deliverable); remaining state is closed out via ``mark_done``.
        """
        async with self._lock:
            prios = list(self._by_question.values())
        for prio in prios:
            if prio.done.is_set():
                continue
            try:
                for sub in list(prio.subscriptions):
                    await prio._fire_subscription(sub)
            except Exception:
                log.exception(
                    "Teardown force-fire failed for prio %s",
                    prio.question_id[:8],
                )
            await prio.mark_done(reason="teardown")
