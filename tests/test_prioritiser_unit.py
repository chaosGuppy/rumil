"""Unit tests for the prioritiser substrate.

These cover the in-memory behaviour of ``Prioritiser``,
``PrioritiserRegistry``, and ``Subscription`` in isolation from the
orchestrator, DB, or LLM. They pin V1's observable invariants and
anchor the V2 actor-model extension points.
"""

import asyncio

import pytest

from rumil.models import CallType
from rumil.prioritisers import Prioritiser, PrioritiserRegistry, Subscription


@pytest.mark.asyncio
async def test_registry_get_or_acquire_first_caller_owns():
    reg = PrioritiserRegistry()
    prio, is_new = await reg.get_or_acquire("q1")
    assert is_new is True
    assert prio.question_id == "q1"


@pytest.mark.asyncio
async def test_registry_get_or_acquire_returns_same_instance():
    reg = PrioritiserRegistry()
    p1, new1 = await reg.get_or_acquire("q1")
    p2, new2 = await reg.get_or_acquire("q1")
    assert new1 is True
    assert new2 is False
    assert p1 is p2


@pytest.mark.asyncio
async def test_registry_different_questions_get_different_prios():
    reg = PrioritiserRegistry()
    p1, _ = await reg.get_or_acquire("q1")
    p2, _ = await reg.get_or_acquire("q2")
    assert p1 is not p2


@pytest.mark.asyncio
async def test_non_scope_dispatch_dedup_first_call_allowed():
    reg = PrioritiserRegistry()
    allowed = await reg.should_execute_non_scope_dispatch("q1", CallType.FIND_CONSIDERATIONS)
    assert allowed is True


@pytest.mark.asyncio
async def test_non_scope_dispatch_dedup_second_call_denied():
    reg = PrioritiserRegistry()
    await reg.should_execute_non_scope_dispatch("q1", CallType.FIND_CONSIDERATIONS)
    denied = await reg.should_execute_non_scope_dispatch("q1", CallType.FIND_CONSIDERATIONS)
    assert denied is False


@pytest.mark.asyncio
async def test_non_scope_dispatch_dedup_different_call_types_independent():
    reg = PrioritiserRegistry()
    await reg.should_execute_non_scope_dispatch("q1", CallType.FIND_CONSIDERATIONS)
    allowed = await reg.should_execute_non_scope_dispatch("q1", CallType.ASSESS)
    assert allowed is True


@pytest.mark.asyncio
async def test_non_scope_dispatch_dedup_different_questions_independent():
    reg = PrioritiserRegistry()
    await reg.should_execute_non_scope_dispatch("q1", CallType.FIND_CONSIDERATIONS)
    allowed = await reg.should_execute_non_scope_dispatch("q2", CallType.FIND_CONSIDERATIONS)
    assert allowed is True


@pytest.mark.asyncio
async def test_prioritiser_mark_done_sets_event():
    prio = Prioritiser("q1")
    assert not prio.done.is_set()
    await prio.mark_done()
    assert prio.done.is_set()


@pytest.mark.asyncio
async def test_prioritiser_await_completion_unblocks_on_mark_done():
    prio = Prioritiser("q1")

    async def completer():
        await asyncio.sleep(0.01)
        await prio.mark_done()

    await asyncio.gather(prio.await_completion(), completer())


@pytest.mark.asyncio
async def test_prioritiser_receive_budget_accumulates():
    prio = Prioritiser("q1")
    await prio.receive_budget(5)
    await prio.receive_budget(3)
    assert prio.budget == 8


@pytest.mark.asyncio
async def test_prioritiser_subscribe_fires_immediately_if_threshold_already_met():
    prio = Prioritiser("q1")
    await prio.on_dispatch_completed(cost=5, delivered_call_id="call-a")
    future = await prio.subscribe(threshold=3)
    assert future.done()
    assert future.result() == "call-a"


@pytest.mark.asyncio
async def test_prioritiser_subscribe_fires_after_threshold_crossed():
    prio = Prioritiser("q1")
    future = await prio.subscribe(threshold=5)
    assert not future.done()
    await prio.on_dispatch_completed(cost=3)
    assert not future.done()
    await prio.on_dispatch_completed(cost=3, delivered_call_id="call-b")
    assert future.done()
    assert future.result() == "call-b"


@pytest.mark.asyncio
async def test_prioritiser_mark_done_resolves_pending_subscriptions():
    prio = Prioritiser("q1")
    f1 = await prio.subscribe(threshold=100)
    f2 = await prio.subscribe(threshold=200)
    await prio.mark_done(delivered_call_id="done-call")
    assert f1.done() and f2.done()
    assert f1.result() == "done-call"
    assert f2.result() == "done-call"


@pytest.mark.asyncio
async def test_prioritiser_on_dispatch_completed_decrements_budget():
    prio = Prioritiser("q1")
    await prio.receive_budget(10)
    await prio.on_dispatch_completed(cost=4)
    assert prio.budget == 6
    assert prio.cumulative_spent == 4


@pytest.mark.asyncio
async def test_prioritiser_budget_cannot_go_negative():
    prio = Prioritiser("q1")
    await prio.receive_budget(2)
    await prio.on_dispatch_completed(cost=5)
    assert prio.budget == 0
    assert prio.cumulative_spent == 5


@pytest.mark.asyncio
async def test_registry_teardown_resolves_pending_prios():
    reg = PrioritiserRegistry()
    prio, _ = await reg.get_or_acquire("q1")
    assert not prio.done.is_set()
    await reg.teardown()
    assert prio.done.is_set()


@pytest.mark.asyncio
async def test_subscription_is_ready():
    loop = asyncio.get_running_loop()
    sub = Subscription(trigger_threshold=10, future=loop.create_future())
    assert sub.is_ready(10) is True
    assert sub.is_ready(15) is True
    assert sub.is_ready(9) is False
