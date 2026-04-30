"""Regression: WebResearchLoop must record CREATE_CLAIM moves into MoveState.

The earlier wrapper rebuilt the create_claim Tool with a custom fn that bypassed
MoveDef.bind, leaving infra.state.moves empty even when the model successfully
created claim pages. That broke trace UI's MovesExecutedEvent and made the
closing-review prompt internally contradictory (claimed "no moves" while listing
the created pages directly below).
"""

import time

import pytest_asyncio
from anthropic.types import Message, TextBlock, ToolUseBlock, Usage

from rumil.calls.page_creators import WebResearchLoop
from rumil.calls.stages import CallInfra, ContextResult
from rumil.llm import APIResponse
from rumil.models import (
    Call,
    CallStatus,
    CallType,
    MoveType,
    Page,
    PageLayer,
    PageType,
    Workspace,
)
from rumil.moves.base import MoveState
from rumil.scraper import ScrapedPage
from rumil.tracing.tracer import CallTrace


@pytest_asyncio.fixture
async def web_question(tmp_db):
    page = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="What did the Apollo 11 mission accomplish?",
        headline="What did the Apollo 11 mission accomplish?",
    )
    await tmp_db.save_page(page)
    return page


@pytest_asyncio.fixture
async def web_research_call(tmp_db, web_question):
    call = Call(
        call_type=CallType.WEB_RESEARCH,
        workspace=Workspace.RESEARCH,
        scope_page_id=web_question.id,
        status=CallStatus.PENDING,
    )
    await tmp_db.save_call(call)
    return call


def _tool_use_response(tool_use_id: str, claim_args: dict) -> APIResponse:
    msg = Message(
        id="msg_1",
        type="message",
        role="assistant",
        model="claude-test",
        content=[
            ToolUseBlock(type="tool_use", id=tool_use_id, name="create_claim", input=claim_args)
        ],
        stop_reason="tool_use",
        stop_sequence=None,
        usage=Usage(input_tokens=100, output_tokens=50),
    )
    return APIResponse(message=msg, duration_ms=10)


def _end_turn_response() -> APIResponse:
    msg = Message(
        id="msg_2",
        type="message",
        role="assistant",
        model="claude-test",
        content=[TextBlock(type="text", text="Done.")],
        stop_reason="end_turn",
        stop_sequence=None,
        usage=Usage(input_tokens=110, output_tokens=10),
    )
    return APIResponse(message=msg, duration_ms=10)


async def test_create_claim_tool_use_records_a_move(
    tmp_db,
    web_question,
    web_research_call,
    mocker,
):
    """A single create_claim tool use must produce a Move on infra.state.moves."""
    mocker.patch(
        "rumil.settings.Settings.require_anthropic_key",
        return_value="test-key",
    )
    mocker.patch(
        "rumil.moves.create_claim.scrape_url",
        return_value=ScrapedPage(
            url="https://example.com/apollo",
            title="Apollo 11",
            content="Apollo 11 was the first crewed mission to land on the Moon, in 1969.",
            fetched_at=str(time.time()),
        ),
    )
    mocker.patch(
        "rumil.calls.page_creators.call_anthropic_api",
        side_effect=[
            _tool_use_response(
                tool_use_id="toolu_1",
                claim_args={
                    "headline": "Apollo 11 was the first crewed lunar landing",
                    "content": (
                        "Apollo 11 landed on the Moon in July 1969 [https://example.com/apollo]."
                    ),
                    "credence": 9,
                    "credence_reasoning": "Well-documented historical event.",
                    "robustness": 5,
                    "robustness_reasoning": "Backed by primary sources.",
                    "source_urls": ["https://example.com/apollo"],
                    "links": [
                        {
                            "question_id": web_question.id,
                            "strength": 4,
                            "reasoning": "Directly answers the question.",
                        }
                    ],
                },
            ),
            _end_turn_response(),
        ],
    )

    state = MoveState(web_research_call, tmp_db)
    trace = CallTrace(web_research_call.id, tmp_db)
    infra = CallInfra(
        question_id=web_question.id,
        call=web_research_call,
        db=tmp_db,
        trace=trace,
        state=state,
    )
    context = ContextResult(context_text="ctx", working_page_ids=[web_question.id])

    loop = WebResearchLoop()
    result = await loop.update_workspace(infra, context)

    assert len(result.moves) == 1
    assert result.moves[0].move_type == MoveType.CREATE_CLAIM
    assert state.moves == result.moves
    assert len(state.move_created_ids) == 1
    assert state.move_created_ids[0], "Move should have at least one created page recorded"
    assert state.last_created_id is not None

    claim_pages = [
        pid
        for pid in result.created_page_ids
        if (await tmp_db.get_page(pid)).page_type == PageType.CLAIM
    ]
    source_pages = [
        pid
        for pid in result.created_page_ids
        if (await tmp_db.get_page(pid)).page_type == PageType.SOURCE
    ]
    assert len(claim_pages) == 1
    assert len(source_pages) == 1
