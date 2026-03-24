"""Test that create_claim fails with an instructive error when a source URL cannot be scraped."""

from anthropic.types import ToolUseBlock

from rumil.calls.common import execute_tool_uses
from rumil.calls.page_creators import WebResearchLoop
from rumil.calls.stages import CallInfra
from rumil.llm import Tool
from rumil.moves.base import MoveState
from rumil.tracing.tracer import CallTrace
from rumil.models import (
    Call,
    CallStatus,
    CallType,
    Workspace,
)

import pytest


@pytest.fixture
def fake_create_claim_tool():
    async def create_claim(inp: dict) -> str:
        return "claim created"

    return Tool(
        name="create_claim",
        description="Create a claim",
        input_schema={"type": "object", "properties": {}},
        fn=create_claim,
    )


def _make_infra(call: Call, db, question_id: str) -> CallInfra:
    return CallInfra(
        question_id=question_id,
        call=call,
        db=db,
        trace=CallTrace(call_id=call.id, db=db),
        state=MoveState(call=call, db=db),
    )


async def test_create_claim_errors_when_source_url_unscrapable(
    tmp_db,
    question_page,
    fake_create_claim_tool,
    mocker,
):
    call = Call(
        call_type=CallType.WEB_RESEARCH,
        workspace=Workspace.RESEARCH,
        scope_page_id=question_page.id,
        status=CallStatus.PENDING,
    )
    await tmp_db.save_call(call)

    loop = WebResearchLoop()
    infra = _make_infra(call, tmp_db, question_page.id)

    mocker.patch(
        "rumil.calls.page_creators.scrape_url",
        return_value=None,
    )

    wrapped_tools = loop._wrap_create_claim([fake_create_claim_tool], infra)
    tool_fns = {t.name: t.fn for t in wrapped_tools}

    tool_use = ToolUseBlock(
        id="test_tool_use_1",
        type="tool_use",
        name="create_claim",
        input={
            "headline": "Some claim",
            "content": "Claim content",
            "source_urls": ["https://unreachable.example.com/article"],
        },
    )

    _, tool_results = await execute_tool_uses([tool_use], tool_fns)

    assert len(tool_results) == 1
    result = tool_results[0]
    assert result["is_error"] is True
    assert "unreachable.example.com" in result["content"]
    assert "different" in result["content"].lower()


async def test_create_claim_succeeds_when_no_source_urls(
    tmp_db,
    question_page,
    fake_create_claim_tool,
):
    call = Call(
        call_type=CallType.WEB_RESEARCH,
        workspace=Workspace.RESEARCH,
        scope_page_id=question_page.id,
        status=CallStatus.PENDING,
    )
    await tmp_db.save_call(call)

    loop = WebResearchLoop()
    infra = _make_infra(call, tmp_db, question_page.id)

    wrapped_tools = loop._wrap_create_claim([fake_create_claim_tool], infra)
    tool_fns = {t.name: t.fn for t in wrapped_tools}

    tool_use = ToolUseBlock(
        id="test_tool_use_2",
        type="tool_use",
        name="create_claim",
        input={
            "headline": "Some claim",
            "content": "Claim content",
        },
    )

    _, tool_results = await execute_tool_uses([tool_use], tool_fns)

    assert len(tool_results) == 1
    result = tool_results[0]
    assert "is_error" not in result
    assert result["content"] == "claim created"
