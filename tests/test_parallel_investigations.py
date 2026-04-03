"""Test that investigate_question dispatches run concurrently."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rumil.clean.feedback import _make_investigation_tools
from rumil.models import (
    Call,
    CallStatus,
    CallType,
    Page,
    PageLayer,
    PageType,
    Workspace,
)


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
            assert "dispatched" in text.lower(), (
                f"Expected dispatch confirmation, got: {text}"
            )

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
