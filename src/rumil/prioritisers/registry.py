"""PrioritiserRegistry: per-DB dedup of prioritisation work.

Shared across all forks of a root DB. For V1 the registry supports:

* ``get_or_acquire(question_id)`` — returns ``(Prioritiser, is_new)``.
  ``is_new=True`` means the caller owns the prioritiser and should run
  its body. ``is_new=False`` means a Prioritiser already exists and the
  caller should ``await prio.await_completion()``.
* ``should_execute_non_scope_dispatch(target_q, call_type)`` — returns
  ``True`` on first call for a given ``(target_q, call_type)`` pair and
  ``False`` on subsequent calls. Used to dedup cross-parent dispatches
  on a shared child.

V2 will add ``recurse(from_prio, to_question_id, budget)`` as a proper
transfer+subscribe primitive, plus subscription-without-transfer as an
LLM-exposed move.
"""

import asyncio
import logging

from rumil.models import CallType
from rumil.prioritisers.prioritiser import Prioritiser

log = logging.getLogger(__name__)


class PrioritiserRegistry:
    def __init__(self) -> None:
        self._by_question: dict[str, Prioritiser] = {}
        self._non_scope_dispatched: set[tuple[str, str]] = set()
        self._lock: asyncio.Lock = asyncio.Lock()

    async def get_or_acquire(
        self,
        question_id: str,
        kind: str = "question",
    ) -> tuple[Prioritiser, bool]:
        async with self._lock:
            existing = self._by_question.get(question_id)
            if existing is not None:
                return existing, False
            prio = Prioritiser(question_id, kind=kind)
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

    async def teardown(self) -> None:
        """Resolve any still-pending prioritisers so parents don't hang."""
        async with self._lock:
            prios = list(self._by_question.values())
        for prio in prios:
            if not prio.done.is_set():
                await prio.mark_done()
