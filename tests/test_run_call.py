"""Tests for run_call — the core entry point for LLM-driven workspace calls."""

import pytest

from differential.calls.common import RunCallResult, run_call
from differential.models import CallType, MoveType
from differential.moves.base import MoveState


@pytest.mark.llm
def test_scout_run_call(tmp_db, question_page, scout_call):
    """A scout call creates pages, records valid moves, and persists them."""
    context = f"## Question\n\nID: `{question_page.id}`\n\n{question_page.content}\n"
    task = (
        "Scout this question. Create at least one claim "
        "as a consideration.\n\n"
        f"Question ID: `{question_page.id}`"
    )

    result = run_call(CallType.SCOUT, task, context, scout_call, tmp_db, max_rounds=3)

    assert isinstance(result, RunCallResult)
    assert len(result.moves) > 0
    assert len(result.created_page_ids) > 0

    for move in result.moves:
        assert move.move_type in MoveType

    for page_id in result.created_page_ids:
        page = tmp_db.get_page(page_id)
        assert page is not None
        assert page.content != ""


@pytest.mark.llm
def test_prioritization_produces_dispatches(tmp_db, question_page, prioritization_call):
    """A prioritization call should produce at least one dispatch."""
    context = (
        f"## Questions\n\n"
        f"- `{question_page.id[:8]}`: {question_page.summary} (0 considerations)\n"
    )
    task = (
        "You have a budget of **2 research calls** to allocate.\n\n"
        f"Scope question ID: `{question_page.id}`\n\n"
        "Dispatch scout or assess calls for this question."
    )

    result = run_call(
        CallType.PRIORITIZATION,
        task,
        context,
        prioritization_call,
        tmp_db,
        max_rounds=3,
    )

    assert isinstance(result, RunCallResult)
    assert len(result.dispatches) > 0
    assert result.dispatches[0].call_type is not None


def test_available_moves_restricts_tools(tmp_db, scout_call):
    """When available_moves is restricted, only those move types are bound."""
    allowed = [MoveType.CREATE_CLAIM, MoveType.LOAD_PAGE]
    state = MoveState(scout_call, tmp_db)
    from differential.moves.registry import MOVES

    tools = [MOVES[mt].bind(state) for mt in allowed]

    tool_names = {t.name for t in tools}
    assert tool_names == {"create_claim", "load_page"}
    assert len(tools) == 2
