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


class PrioritiserRegistry:
    def __init__(self) -> None:
        self._by_question: dict[str, Prioritiser] = {}
        self._non_scope_dispatched: set[tuple[str, str]] = set()
        self._lock: asyncio.Lock = asyncio.Lock()
        self._pending_trace_tasks: set[asyncio.Task] = set()

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
                    to_question_id=target_question_id,
                    amount=budget,
                )
            )
        await prio.receive_budget(budget)
        async with prio._lock:
            threshold = prio.cumulative_spent + prio.budget
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
            await prio.mark_done()
