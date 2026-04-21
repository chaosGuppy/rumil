"""Unit tests for the prioritiser substrate.

These cover the in-memory behaviour of ``Prioritiser``,
``PrioritiserRegistry``, and ``Subscription`` in isolation from the
orchestrator, DB, or LLM. They pin V1's observable invariants and
anchor the V2 actor-model extension points.
"""

import asyncio

import pytest

from rumil.llm import _experimental_scout_budget as _exp_budget_cv
from rumil.llm import (
    reset_experimental_scout_budget,
    set_experimental_scout_budget,
)
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


class _RecordingPrioritiser(Prioritiser):
    """Test double that records _run_round invocations and consumes budget.

    Each round consumes ``per_round_cost`` (default: full budget). Tests
    can inspect ``rounds`` to see what budget each round was called with.
    """

    def __init__(
        self,
        question_id: str,
        kind: str = "question",
        per_round_cost: int | None = None,
        raise_on_round: int | None = None,
    ) -> None:
        super().__init__(question_id, kind=kind)
        self.rounds: list[int] = []
        self.per_round_cost = per_round_cost
        self.raise_on_round = raise_on_round
        self.fire_calls: list[Subscription] = []

    async def _run_round(self, round_budget: int) -> None:
        self.rounds.append(round_budget)
        if self.raise_on_round is not None and len(self.rounds) == self.raise_on_round:
            raise RuntimeError("boom")
        cost = self.per_round_cost if self.per_round_cost is not None else round_budget
        await self.on_dispatch_completed(
            cost=cost,
            delivered_call_id=f"call-round-{len(self.rounds)}",
        )

    async def _fire_subscription(self, subscription: Subscription) -> None:
        self.fire_calls.append(subscription)
        subscription.resolve(self._last_delivered_call_id or "force-fire")


async def _wait_until(predicate, timeout: float = 1.0) -> None:
    """Poll-and-yield until predicate() becomes truthy or timeout expires."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("predicate did not become truthy within timeout")


@pytest.mark.asyncio
async def test_prioritiser_round_loop_runs_round_when_budget_arrives():
    prio = _RecordingPrioritiser("q1")
    await prio.start()
    await prio.receive_budget(7)
    await _wait_until(lambda: prio.rounds == [7])
    await prio.mark_done()
    await asyncio.wait_for(prio.await_completion(), timeout=1.0)
    assert prio.state == "done"


@pytest.mark.asyncio
async def test_prioritiser_round_loop_loops_across_multiple_rounds():
    prio = _RecordingPrioritiser("q1")
    await prio.start()
    await prio.receive_budget(3)
    await _wait_until(lambda: prio.rounds == [3])
    assert not prio.done.is_set()
    await prio.receive_budget(5)
    await _wait_until(lambda: prio.rounds == [3, 5])
    await prio.mark_done()
    await asyncio.wait_for(prio.await_completion(), timeout=1.0)


@pytest.mark.asyncio
async def test_prioritiser_state_transitions_idle_running_done():
    observed: list[str] = []

    class _ObservingPrio(_RecordingPrioritiser):
        async def _run_round(self, round_budget: int) -> None:
            observed.append(self.state)
            await super()._run_round(round_budget)

    prio = _ObservingPrio("q1")
    assert prio.state == "idle"
    await prio.start()
    await prio.receive_budget(4)
    await _wait_until(lambda: prio.rounds == [4])
    await _wait_until(lambda: prio.state == "idle")
    await prio.mark_done()
    await asyncio.wait_for(prio.await_completion(), timeout=1.0)
    assert observed == ["running"]
    assert prio.state == "done"


@pytest.mark.asyncio
async def test_prioritiser_receive_budget_wakes_loop():
    prio = _RecordingPrioritiser("q1")
    await prio.start()
    await asyncio.sleep(0.01)
    assert prio.rounds == []
    await prio.receive_budget(2)
    await _wait_until(lambda: prio.rounds == [2])
    await prio.mark_done()


@pytest.mark.asyncio
async def test_prioritiser_subscribe_fires_when_threshold_met_via_round():
    prio = _RecordingPrioritiser("q1")
    await prio.start()
    future = await prio.subscribe(threshold=5)
    assert not future.done()
    await prio.receive_budget(5)
    result = await asyncio.wait_for(future, timeout=1.0)
    assert result == "call-round-1"
    await prio.mark_done()


@pytest.mark.asyncio
async def test_prioritiser_round_error_marks_done_and_resolves_subscriptions():
    prio = _RecordingPrioritiser("q1", raise_on_round=1)
    f = await prio.subscribe(threshold=100)
    await prio.start()
    await prio.receive_budget(3)
    await asyncio.wait_for(prio.await_completion(), timeout=1.0)
    assert prio.state == "done"
    assert f.done()


@pytest.mark.asyncio
async def test_prioritiser_partial_round_budget_loops_once_spent():
    prio = _RecordingPrioritiser("q1", per_round_cost=1)
    await prio.start()
    await prio.receive_budget(3)
    await _wait_until(lambda: prio.rounds == [3, 2, 1])
    await prio.mark_done()


@pytest.mark.asyncio
async def test_registry_teardown_fires_pending_subscription_via_hook():
    reg = PrioritiserRegistry()
    prio, _ = await reg.get_or_acquire("q1", factory=_RecordingPrioritiser)
    assert isinstance(prio, _RecordingPrioritiser)
    future = await prio.subscribe(threshold=999)
    await reg.teardown()
    assert len(prio.fire_calls) == 1
    assert future.done()
    assert prio.done.is_set()


@pytest.mark.asyncio
async def test_prioritiser_blocks_when_no_budget_and_pending_subscription():
    prio = _RecordingPrioritiser("q1")
    await prio.start()
    f = await prio.subscribe(threshold=5)
    await asyncio.sleep(0.02)
    assert not prio.done.is_set()
    assert not f.done()
    await prio.receive_budget(5)
    await asyncio.wait_for(f, timeout=1.0)
    await prio.mark_done()


@pytest.mark.asyncio
async def test_prioritiser_mark_done_from_outside_stops_loop():
    prio = _RecordingPrioritiser("q1")
    await prio.start()
    f = await prio.subscribe(threshold=999)
    await prio.mark_done(delivered_call_id="forced")
    await asyncio.wait_for(prio.await_completion(), timeout=1.0)
    assert f.done()
    assert f.result() == "forced"


@pytest.mark.asyncio
async def test_registry_recurse_transfers_budget_and_subscribes():
    reg = PrioritiserRegistry()
    future = await reg.recurse("q1", budget=5, factory=_RecordingPrioritiser)
    prio = await reg.get("q1")
    assert prio is not None
    assert isinstance(prio, _RecordingPrioritiser)
    result = await asyncio.wait_for(future, timeout=1.0)
    assert result == "call-round-1"
    assert prio.rounds == [5]
    await prio.mark_done()


@pytest.mark.asyncio
async def test_registry_recurse_into_existing_prio_stacks_budget():
    reg = PrioritiserRegistry()
    f1 = await reg.recurse("q1", budget=3, factory=_RecordingPrioritiser)
    await asyncio.wait_for(f1, timeout=1.0)
    prio = await reg.get("q1")
    assert prio is not None
    assert isinstance(prio, _RecordingPrioritiser)
    assert prio.rounds == [3]
    f2 = await reg.recurse("q1", budget=4, factory=_RecordingPrioritiser)
    result2 = await asyncio.wait_for(f2, timeout=1.0)
    assert result2 == "call-round-2"
    assert prio.rounds == [3, 4]
    await prio.mark_done()


@pytest.mark.asyncio
async def test_registry_recurse_subscription_threshold_waits_for_full_budget():
    reg = PrioritiserRegistry()
    future = await reg.recurse(
        "q1",
        budget=10,
        factory=_RecordingPrioritiser,
    )
    prio = await reg.get("q1")
    assert prio is not None
    assert isinstance(prio, _RecordingPrioritiser)
    prio.per_round_cost = 3
    await asyncio.wait_for(future, timeout=1.0)
    assert prio.cumulative_spent >= 10
    await prio.mark_done()


@pytest.mark.asyncio
async def test_prioritiser_task_exits_when_budget_drains():
    """The round-loop task should exit naturally once budget hits zero.

    Key invariant of the ephemeral-task design: we don't keep an idle task
    alive per node waiting for more budget. The prioritiser *object* stays
    in the registry (state="idle") so subsequent ``receive_budget`` calls
    can respawn a task — but the task itself is gone between rounds.
    """
    prio = _RecordingPrioritiser("q1")
    await prio.start()
    await prio.receive_budget(4)
    await _wait_until(lambda: prio.rounds == [4])
    await _wait_until(lambda: prio._task is not None and prio._task.done())
    assert prio.state == "idle"
    assert not prio.done.is_set()


@pytest.mark.asyncio
async def test_prioritiser_receive_budget_respawns_task_after_drain():
    """Scenario B collision: parent2 transfers budget after parent1's drain.

    After the task exits following budget drain, a fresh ``receive_budget``
    call must respawn a task so the new budget gets processed. The same
    prioritiser object picks up where it left off — ``cumulative_spent``
    carries over, subscriptions added earlier still fire at their
    thresholds.
    """
    prio = _RecordingPrioritiser("q1")
    await prio.start()
    await prio.receive_budget(3)
    await _wait_until(lambda: prio.rounds == [3])
    await _wait_until(lambda: prio._task is not None and prio._task.done())
    first_task = prio._task
    await prio.receive_budget(7)
    await _wait_until(lambda: prio.rounds == [3, 7])
    assert prio._task is not first_task
    assert prio.cumulative_spent == 10
    await prio.mark_done()


@pytest.mark.asyncio
async def test_prioritiser_start_is_idempotent_while_task_running():
    """Repeated ``start()`` calls while a task is running are no-ops.

    Without this, a caller racing ``receive_budget`` with a direct
    ``start()`` (or multiple recurse calls stacking into the same prio)
    could spawn duplicate round-loop tasks that race on the same state.
    """
    release = asyncio.Event()
    entered = asyncio.Event()

    class _BlockingPrio(_RecordingPrioritiser):
        async def _run_round(self, round_budget: int) -> None:
            entered.set()
            await release.wait()
            await super()._run_round(round_budget)

    prio = _BlockingPrio("q1")
    await prio.start()
    await prio.receive_budget(3)
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    assert prio.state == "running"
    task_during_run = prio._task
    await prio.start()
    await prio.start()
    assert prio._task is task_during_run
    release.set()
    await _wait_until(lambda: prio.cumulative_spent == 3)
    await prio.mark_done()


@pytest.mark.asyncio
async def test_experimental_scout_budget_is_task_local_under_gather():
    """Sibling prioritiser rounds running under asyncio.gather must not share contextvar state.

    Regression pin: ``set_experimental_scout_budget`` is a contextvars.ContextVar.
    When two sibling prios each set/reset their own budget inside a round, the
    budget they observe inside their own round must match the one they set,
    not the sibling's. asyncio.Task copies contextvars at spawn time, so each
    gathered coroutine gets its own copy — but we want a unit test to pin this.
    """
    observed: dict[str, list[int | None]] = {"a": [], "b": []}
    barrier = asyncio.Event()

    async def run_one(name: str, budget: int) -> None:
        token = set_experimental_scout_budget(budget)
        try:
            if name == "a":
                await asyncio.sleep(0)
                observed[name].append(_exp_budget_cv.get())
                barrier.set()
                await asyncio.sleep(0)
                observed[name].append(_exp_budget_cv.get())
            else:
                await barrier.wait()
                observed[name].append(_exp_budget_cv.get())
                await asyncio.sleep(0)
                observed[name].append(_exp_budget_cv.get())
        finally:
            reset_experimental_scout_budget(token)

    await asyncio.gather(run_one("a", 5), run_one("b", 11))

    assert observed["a"] == [5, 5]
    assert observed["b"] == [11, 11]
    assert _exp_budget_cv.get() is None


@pytest.mark.asyncio
async def test_registry_teardown_force_fire_produces_deliverable_via_hook():
    """Teardown must route through ``_fire_subscription`` so subclasses can produce a fresh deliverable.

    The V2 intent: even if a subscription threshold hasn't been reached when
    run budget runs out, ``registry.teardown()`` should invoke the subclass
    hook so the parent still receives a usable call id (e.g. the current
    view). This test pins that routing.
    """
    reg = PrioritiserRegistry()
    prio, _ = await reg.get_or_acquire("q1", factory=_RecordingPrioritiser)
    assert isinstance(prio, _RecordingPrioritiser)
    future = await prio.subscribe(threshold=999)
    assert not future.done()
    await reg.teardown()
    result = await asyncio.wait_for(future, timeout=1.0)
    assert result == "force-fire"
    assert len(prio.fire_calls) == 1
    assert prio.done.is_set()


@pytest.mark.asyncio
async def test_registry_teardown_fires_hook_on_each_of_multiple_prios():
    """Every pending prio gets its ``_fire_subscription`` hook called and ends up done."""
    reg = PrioritiserRegistry()
    p1, _ = await reg.get_or_acquire("q1", factory=_RecordingPrioritiser)
    p2, _ = await reg.get_or_acquire("q2", factory=_RecordingPrioritiser)
    p3, _ = await reg.get_or_acquire("q3", factory=_RecordingPrioritiser)
    assert isinstance(p1, _RecordingPrioritiser)
    assert isinstance(p2, _RecordingPrioritiser)
    assert isinstance(p3, _RecordingPrioritiser)
    f1 = await p1.subscribe(threshold=999)
    f2a = await p2.subscribe(threshold=999)
    f2b = await p2.subscribe(threshold=500)

    await reg.teardown()

    assert len(p1.fire_calls) == 1
    assert len(p2.fire_calls) == 2
    assert len(p3.fire_calls) == 0
    assert f1.done() and f2a.done() and f2b.done()
    assert p1.done.is_set() and p2.done.is_set() and p3.done.is_set()


@pytest.mark.asyncio
async def test_prioritiser_state_cycles_idle_running_idle_running_done():
    """Full cycle idle -> running -> idle -> running -> done across two budget grants."""
    observed_states: list[str] = []

    class _StateObservingPrio(_RecordingPrioritiser):
        async def _run_round(self, round_budget: int) -> None:
            observed_states.append(self.state)
            await super()._run_round(round_budget)

    prio = _StateObservingPrio("q1")
    assert prio.state == "idle"
    await prio.start()
    await prio.receive_budget(3)
    await _wait_until(lambda: prio.rounds == [3])
    await _wait_until(lambda: prio.state == "idle")
    await prio.receive_budget(4)
    await _wait_until(lambda: prio.rounds == [3, 4])
    await _wait_until(lambda: prio.state == "idle")
    await prio.mark_done()
    await asyncio.wait_for(prio.await_completion(), timeout=1.0)
    assert observed_states == ["running", "running"]
    assert prio.state == "done"


@pytest.mark.asyncio
async def test_prioritiser_receive_budget_after_done_is_noop():
    """After ``mark_done``, further ``receive_budget`` calls must not respawn the loop."""
    prio = _RecordingPrioritiser("q1")
    await prio.mark_done()
    assert prio.state == "done"
    await prio.receive_budget(10)
    await asyncio.sleep(0.02)
    assert prio.rounds == []
    assert prio.state == "done"
    assert prio._task is None


@pytest.mark.asyncio
async def test_prioritiser_mark_done_during_active_round_terminates_cleanly():
    """``mark_done`` during an in-flight round: loop exits on the next checkpoint, no further rounds."""
    release = asyncio.Event()
    entered = asyncio.Event()

    class _BlockingInRoundPrio(_RecordingPrioritiser):
        async def _run_round(self, round_budget: int) -> None:
            entered.set()
            await release.wait()
            await super()._run_round(round_budget)

    prio = _BlockingInRoundPrio("q1")
    f = await prio.subscribe(threshold=999)
    await prio.start()
    await prio.receive_budget(5)
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    assert prio.state == "running"
    await prio.mark_done(delivered_call_id="forced-during-round")
    await asyncio.wait_for(prio.await_completion(), timeout=1.0)
    release.set()
    task = prio._task
    assert task is not None
    await asyncio.wait_for(task, timeout=1.0)
    assert prio.state == "done"
    assert f.done()
    assert f.result() == "forced-during-round"
    assert prio.rounds == [5]
