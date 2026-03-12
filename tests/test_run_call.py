"""Tests for run_call — the core entry point for LLM-driven workspace calls."""

import pytest

from differential.calls.common import RunCallResult, run_call
from differential.models import CallType, LinkType, MoveType, PageType
from differential.moves.base import MoveState


@pytest.mark.llm
async def test_scout_run_call(tmp_db, question_page, scout_call):
    """A scout call creates pages, records valid moves, and persists them."""
    context = f"## Question\n\nID: `{question_page.id}`\n\n{question_page.content}\n"
    task = (
        "Scout this question. Create at least one claim "
        "as a consideration.\n\n"
        f"Question ID: `{question_page.id}`"
    )

    result = await run_call(CallType.SCOUT, task, context, scout_call, tmp_db, max_rounds=3)

    assert isinstance(result, RunCallResult)
    assert len(result.moves) > 0
    assert len(result.created_page_ids) > 0

    for move in result.moves:
        assert move.move_type in MoveType

    for page_id in result.created_page_ids:
        page = await tmp_db.get_page(page_id)
        assert page is not None
        assert page.content != ""


@pytest.mark.llm
async def test_prioritization_produces_dispatches(tmp_db, question_page, prioritization_call):
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

    result = await run_call(
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


async def test_available_moves_restricts_tools(tmp_db, scout_call):
    """When available_moves is restricted, only those move types are bound."""
    allowed = [MoveType.CREATE_CLAIM, MoveType.LOAD_PAGE]
    state = MoveState(scout_call, tmp_db)
    from differential.moves.registry import MOVES

    tools = [MOVES[mt].bind(state) for mt in allowed]

    tool_names = {t.name for t in tools}
    assert tool_names == {"create_claim", "load_page"}
    assert len(tools) == 2


@pytest.mark.llm
async def test_create_claim_with_inline_links(tmp_db, question_page, scout_call):
    """The LLM should create a claim linked as a consideration in a single tool call."""
    context = (
        f"## Question\n\n"
        f"ID: `{question_page.id[:8]}`\n\n"
        f"{question_page.content}\n"
    )
    task = (
        'Create one claim that supports this question and link it '
        'as a consideration.\n\n'
        f'Question ID: `{question_page.id[:8]}`'
    )

    result = await run_call(
        CallType.SCOUT,
        task,
        context,
        scout_call,
        tmp_db,
        available_moves=[MoveType.CREATE_CLAIM, MoveType.LOAD_PAGE],
        max_rounds=2,
    )

    assert len(result.created_page_ids) >= 1

    claim_id = result.created_page_ids[0]
    claim = await tmp_db.get_page(claim_id)
    assert claim is not None
    assert claim.page_type is PageType.CLAIM

    links = await tmp_db.get_links_from(claim_id)
    consideration_links = [l for l in links if l.link_type == LinkType.CONSIDERATION]
    assert len(consideration_links) >= 1, (
        'Expected at least one consideration link from the claim to the question'
    )
    assert consideration_links[0].to_page_id == question_page.id


@pytest.mark.llm
async def test_create_question_with_inline_links(tmp_db, question_page, scout_call):
    """The LLM should create a sub-question linked to its parent in a single tool call."""
    context = (
        f"## Question\n\n"
        f"ID: `{question_page.id[:8]}`\n\n"
        f"{question_page.content}\n"
    )
    task = (
        'Create one sub-question that breaks down the question above '
        'and link it as a child.\n\n'
        f'Parent question ID: `{question_page.id[:8]}`'
    )

    result = await run_call(
        CallType.SCOUT,
        task,
        context,
        scout_call,
        tmp_db,
        available_moves=[MoveType.CREATE_QUESTION, MoveType.LOAD_PAGE],
        max_rounds=2,
    )

    assert len(result.created_page_ids) >= 1

    child_id = result.created_page_ids[0]
    child = await tmp_db.get_page(child_id)
    assert child is not None
    assert child.page_type is PageType.QUESTION

    links = await tmp_db.get_links_to(child_id)
    child_links = [l for l in links if l.link_type == LinkType.CHILD_QUESTION]
    assert len(child_links) >= 1, (
        'Expected at least one child_question link from the parent to the new question'
    )
    assert child_links[0].from_page_id == question_page.id


@pytest.mark.llm
async def test_create_subquestion_with_inline_dispatches(
    tmp_db, question_page, prioritization_call,
):
    """Prioritization should create subquestions with inline dispatches."""
    context = (
        f"## Questions\n\n"
        f"- `{question_page.id[:8]}`: {question_page.summary} (0 considerations)\n"
    )
    task = (
        "You have a budget of **2 research calls** to allocate.\n\n"
        f"Scope question ID: `{question_page.id}`\n\n"
        "Create a subquestion using `create_subquestion` and dispatch a scout on it "
        "using the `dispatches` field. Link it as a child of the scope question "
        "using the `links` field."
    )

    result = await run_call(
        CallType.PRIORITIZATION,
        task,
        context,
        prioritization_call,
        tmp_db,
    )

    assert len(result.created_page_ids) >= 1
    assert len(result.dispatches) >= 1
