"""Test that investigate_question dispatches run concurrently."""

import asyncio
import time
from collections import defaultdict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rumil.calls.common import RunCallResult
from rumil.clean.feedback import _make_investigation_tools
from rumil.models import (
    Call,
    CallStatus,
    CallType,
    Dispatch,
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    RecurseDispatchPayload,
    ScoutDispatchPayload,
    Workspace,
)
from rumil.orchestrators.two_phase import TwoPhaseOrchestrator


def _fake_page(page_id: str = "aaaa1111", headline: str = "Test Q") -> Page:
    return Page(
        id=page_id,
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=headline,
        headline=headline,
    )


def _fake_call() -> Call:
    return Call(
        call_type=CallType.FEEDBACK_UPDATE,
        workspace=Workspace.RESEARCH,
        scope_page_id="aaaa1111",
        status=CallStatus.RUNNING,
    )


def _mock_db() -> MagicMock:
    db = MagicMock()
    db.resolve_page_id = AsyncMock(side_effect=lambda pid: pid)
    db.get_page = AsyncMock(return_value=_fake_page())
    db.save_page = AsyncMock()
    db.get_judgements_for_question = AsyncMock(return_value=[])
    return db


SLEEP_SECONDS = 0.3


@pytest.fixture
def investigation_tools():
    """Create investigation tools with mocked DB and orchestrator."""
    call = _fake_call()
    db = _mock_db()
    investigate_tool, collect_tool = _make_investigation_tools(
        call=call,
        db=db,
        broadcaster=None,
        investigation_budget=100,
    )
    return investigate_tool, collect_tool


async def test_investigations_run_concurrently(investigation_tools):
    """Dispatching N investigations then collecting should take ~1x sleep, not Nx."""
    investigate_tool, collect_tool = investigation_tools
    num_investigations = 3

    with (
        patch("rumil.clean.feedback.link_pages", new_callable=AsyncMock),
        patch("rumil.clean.feedback.ExperimentalOrchestrator") as MockOrch,
    ):
        mock_instance = MagicMock()
        mock_instance.create_initial_call = AsyncMock(return_value="child-call-id-1234")

        async def slow_run(question_id: str) -> None:
            await asyncio.sleep(SLEEP_SECONDS)

        mock_instance.run = AsyncMock(side_effect=slow_run)
        mock_instance._parent_call_id = None
        MockOrch.return_value = mock_instance

        for i in range(num_investigations):
            result = await investigate_tool.handler(
                {
                    "question_id": f"q{i:07d}",
                    "parent_question_id": "aaaa1111",
                    "budget": 5,
                }
            )
            text = result["content"][0]["text"]
            assert "dispatched" in text.lower(), f"Expected dispatch confirmation, got: {text}"

        t0 = time.monotonic()
        result = await collect_tool.handler({})
        elapsed = time.monotonic() - t0

        text = result["content"][0]["text"]
        assert "complete" in text.lower(), f"Expected completion summaries, got: {text}"
        assert text.count("Investigation complete") == num_investigations

    max_serial_time = SLEEP_SECONDS * num_investigations
    assert elapsed < max_serial_time * 0.8, (
        f"Investigations appear serial: {elapsed:.2f}s >= "
        f"{max_serial_time * 0.8:.2f}s (80% of serial time {max_serial_time:.2f}s). "
        f"Expected ~{SLEEP_SECONDS:.2f}s for parallel execution."
    )


async def test_collect_with_no_pending(investigation_tools):
    """collect_investigations with nothing pending returns a no-op message."""
    _, collect_tool = investigation_tools
    result = await collect_tool.handler({})
    text = result["content"][0]["text"]
    assert "no pending" in text.lower()


async def test_validation_failure_does_not_leak_budget(investigation_tools):
    """If validation fails (e.g. missing question_id), budget must not be consumed."""
    investigate_tool, _ = investigation_tools

    result = await investigate_tool.handler(
        {
            "question_id": "",
            "headline": "",
            "parent_question_id": "aaaa1111",
            "budget": 50,
        }
    )
    text = result["content"][0]["text"]
    assert "error" in text.lower()

    with (
        patch("rumil.clean.feedback.link_pages", new_callable=AsyncMock),
        patch("rumil.clean.feedback.ExperimentalOrchestrator") as MockOrch,
    ):
        mock_instance = MagicMock()
        mock_instance.create_initial_call = AsyncMock(return_value="child-id")
        mock_instance.run = AsyncMock()
        mock_instance._parent_call_id = None
        MockOrch.return_value = mock_instance

        result = await investigate_tool.handler(
            {
                "question_id": "q0000001",
                "parent_question_id": "aaaa1111",
                "budget": 100,
            }
        )
        text = result["content"][0]["text"]
        assert "dispatched" in text.lower(), (
            f"Full budget should still be available after validation failure, got: {text}"
        )


async def test_budget_rejection(investigation_tools):
    """Requesting more than available budget is rejected immediately."""
    investigate_tool, _ = investigation_tools

    with patch("rumil.clean.feedback.link_pages", new_callable=AsyncMock):
        result = await investigate_tool.handler(
            {
                "question_id": "q0000001",
                "parent_question_id": "aaaa1111",
                "budget": 999,
            }
        )
        text = result["content"][0]["text"]
        assert "rejected" in text.lower()


async def test_budget_deducted_across_dispatches(investigation_tools):
    """Budget pool is shared across dispatches and enforced correctly."""
    investigate_tool, collect_tool = investigation_tools

    with (
        patch("rumil.clean.feedback.link_pages", new_callable=AsyncMock),
        patch("rumil.clean.feedback.ExperimentalOrchestrator") as MockOrch,
    ):
        mock_instance = MagicMock()
        mock_instance.create_initial_call = AsyncMock(return_value="child-id")
        mock_instance.run = AsyncMock()
        mock_instance._parent_call_id = None
        MockOrch.return_value = mock_instance

        await investigate_tool.handler(
            {
                "question_id": "q0000001",
                "parent_question_id": "aaaa1111",
                "budget": 60,
            }
        )
        await investigate_tool.handler(
            {
                "question_id": "q0000002",
                "parent_question_id": "aaaa1111",
                "budget": 35,
            }
        )
        result = await investigate_tool.handler(
            {
                "question_id": "q0000003",
                "parent_question_id": "aaaa1111",
                "budget": 10,
            }
        )
        text = result["content"][0]["text"]
        assert "rejected" in text.lower(), (
            f"Third dispatch should be rejected (budget 60+35+10>100), got: {text}"
        )

        await collect_tool.handler({})


def _scout_dispatch(question_id: str, reason: str = "") -> Dispatch:
    return Dispatch(
        call_type=CallType.FIND_CONSIDERATIONS,
        payload=ScoutDispatchPayload(
            question_id=question_id,
            max_rounds=1,
            reason=reason,
        ),
    )


async def _make_question(db, headline: str) -> Page:
    page = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=headline,
        headline=headline,
    )
    await db.save_page(page)
    return page


async def test_concurrent_recursive_children_dont_overspend_global_budget(tmp_db, mocker):
    """Two concurrent recurse children with caps summing above remaining must not overspend.

    Pins the invariant that ``_consumed`` tracked locally per orchestrator
    does not allow siblings to collectively overdraw the shared global pool.
    If a future rearch holds stale per-orchestrator consumption counters, this
    test should fail.
    """
    parent_q = await _make_question(tmp_db, "Parent Q")
    child_a = await _make_question(tmp_db, "Child A")
    child_b = await _make_question(tmp_db, "Child B")
    for child in (child_a, child_b):
        await tmp_db.save_link(
            PageLink(
                from_page_id=parent_q.id,
                to_page_id=child.id,
                link_type=LinkType.CHILD_QUESTION,
            )
        )

    await tmp_db.init_budget(15)
    initial_total = 15

    scripts_by_scope: dict[str, list[RunCallResult]] = {
        parent_q.id: [
            RunCallResult(dispatches=[_scout_dispatch(parent_q.id, "seed")]),
            RunCallResult(
                dispatches=[
                    Dispatch(
                        call_type=CallType.PRIORITIZATION,
                        payload=RecurseDispatchPayload(
                            question_id=child_a.id,
                            budget=10,
                            reason="drill A",
                        ),
                    ),
                    Dispatch(
                        call_type=CallType.PRIORITIZATION,
                        payload=RecurseDispatchPayload(
                            question_id=child_b.id,
                            budget=10,
                            reason="drill B",
                        ),
                    ),
                ]
            ),
            RunCallResult(dispatches=[]),
        ],
        child_a.id: [
            RunCallResult(dispatches=[_scout_dispatch(child_a.id, f"a{i}") for i in range(10)]),
            RunCallResult(dispatches=[]),
        ],
        child_b.id: [
            RunCallResult(dispatches=[_scout_dispatch(child_b.id, f"b{i}") for i in range(10)]),
            RunCallResult(dispatches=[]),
        ],
    }
    call_counts: dict[str, int] = defaultdict(int)
    dispatched: list[dict] = []

    async def _fake_prio(task, context_text, call, db, **kwargs):
        scope = call.scope_page_id
        idx = call_counts[scope]
        call_counts[scope] += 1
        scripts = scripts_by_scope.get(scope, [])
        if idx < len(scripts):
            return scripts[idx]
        return RunCallResult()

    mocker.patch(
        "rumil.orchestrators.two_phase.run_prioritization_call",
        side_effect=_fake_prio,
    )

    async def _simulate(call_type, question_id, **kwargs):
        force = kwargs.get("force", False)
        ok = await tmp_db.consume_budget(1)
        if not ok:
            if force:
                await tmp_db.add_budget(1)
                ok = await tmp_db.consume_budget(1)
            if not ok:
                return None
        call = await tmp_db.create_call(
            call_type,
            scope_page_id=question_id,
            parent_call_id=kwargs.get("parent_call_id"),
            call_id=kwargs.get("call_id"),
            sequence_id=kwargs.get("sequence_id"),
            sequence_position=kwargs.get("sequence_position"),
            workspace=Workspace.RESEARCH,
        )
        call.status = CallStatus.COMPLETE
        await tmp_db.save_call(call)
        dispatched.append(
            {"question_id": question_id, "force": force, "call_type": call_type.value}
        )
        return call.id

    async def _fake_fc(question_id, db, **kwargs):
        cid = await _simulate(CallType.FIND_CONSIDERATIONS, question_id, **kwargs)
        return (0, [cid] if cid else [])

    mocker.patch(
        "rumil.orchestrators.dispatch_handlers.find_considerations_until_done",
        side_effect=_fake_fc,
    )
    mocker.patch(
        "rumil.orchestrators.two_phase.score_items_sequentially",
        return_value=[],
    )

    async def _noop_view(*args, **kwargs):
        return None

    mocker.patch(
        "rumil.views.sectioned.create_view_for_question",
        side_effect=_noop_view,
    )
    mocker.patch(
        "rumil.views.sectioned.update_view_for_question",
        side_effect=_noop_view,
    )

    orch = TwoPhaseOrchestrator(tmp_db)
    await orch.run(parent_q.id)

    non_forced = [d for d in dispatched if not d["force"]]
    assert len(non_forced) <= initial_total, (
        f"non-forced dispatches ({len(non_forced)}) exceeded initial global pool "
        f"({initial_total}) — concurrent children overdrew the shared budget"
    )

    total, used = await tmp_db.get_budget()
    assert used <= total, f"used={used} exceeded total={total}"
    forced_count = sum(1 for d in dispatched if d["force"])
    assert total == initial_total + forced_count, (
        f"total={total} should equal initial {initial_total} + forced {forced_count}"
    )
