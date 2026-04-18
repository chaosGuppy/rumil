"""Tests for the get_view and get_view_item chat tools.

The chat backend wraps build_view in tool handlers so the model can fetch
the distilled view of a question directly. These tests exercise the tool
handlers via _execute_tool against a seeded DB — no LLM calls.
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio

from rumil.api import chat as chat_module
from rumil.api.chat import TOOLS, _execute_tool
from rumil.models import (
    ConsiderationDirection,
    LinkRole,
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)


@pytest_asyncio.fixture
async def seeded_question(tmp_db):
    """A question with a core finding, a live hypothesis, and a child question."""
    question = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Does tool-layer view awareness improve chat quality?",
        headline="Does tool-layer view awareness improve chat quality?",
    )
    await tmp_db.save_page(question)

    core_finding = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Models fetch structured views faster than reconstructing them.",
        headline="Structured views beat reconstruction",
        credence=8,
        robustness=4,
        importance=1,
    )
    await tmp_db.save_page(core_finding)
    await tmp_db.save_link(
        PageLink(
            from_page_id=core_finding.id,
            to_page_id=question.id,
            link_type=LinkType.CONSIDERATION,
            strength=3.0,
            direction=ConsiderationDirection.SUPPORTS,
            role=LinkRole.DIRECT,
        )
    )

    live_hypothesis = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Adding get_view may just duplicate what search_workspace covers.",
        headline="Duplication risk vs search_workspace",
        credence=5,
        robustness=2,
        importance=2,
    )
    await tmp_db.save_page(live_hypothesis)
    await tmp_db.save_link(
        PageLink(
            from_page_id=live_hypothesis.id,
            to_page_id=question.id,
            link_type=LinkType.CONSIDERATION,
            strength=2.0,
            direction=ConsiderationDirection.OPPOSES,
            role=LinkRole.DIRECT,
        )
    )

    child_q = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="How much payload bloat does the tool add?",
        headline="How much payload bloat does the tool add?",
    )
    await tmp_db.save_page(child_q)
    await tmp_db.save_link(
        PageLink(
            from_page_id=question.id,
            to_page_id=child_q.id,
            link_type=LinkType.CHILD_QUESTION,
            reasoning="Scope check",
            role=LinkRole.DIRECT,
        )
    )

    return {
        "question": question,
        "core_finding": core_finding,
        "live_hypothesis": live_hypothesis,
        "child": child_q,
    }


async def test_get_view_returns_structured_payload(tmp_db, seeded_question):
    question = seeded_question["question"]
    result_str = await _execute_tool(
        "get_view",
        {"question_id": question.id[:8]},
        tmp_db,
        scope_question_id=question.id,
    )
    payload = json.loads(result_str)

    assert payload["question_id"] == question.id[:8]
    assert payload["question_full_id"] == question.id
    assert payload["question_headline"] == question.headline

    assert "sections" in payload
    assert len(payload["sections"]) >= 1
    section_names = {s["name"] for s in payload["sections"]}
    assert "core_findings" in section_names

    core_section = next(s for s in payload["sections"] if s["name"] == "core_findings")
    assert len(core_section["items"]) == 1
    core_item = core_section["items"][0]
    assert core_item["id"] == seeded_question["core_finding"].id[:8]
    assert core_item["headline"] == "Structured views beat reconstruction"
    assert core_item["credence"] == 8
    assert core_item["robustness"] == 4
    assert core_item["importance"] == 1
    assert core_item["direction"] == "supports"
    assert core_item["section"] == "core_findings"
    assert "content" not in core_item

    health = payload["health"]
    assert "total_pages" in health
    assert "missing_credence" in health
    assert "missing_importance" in health
    assert "child_questions_without_judgements" in health
    assert "max_depth" in health
    assert health["total_pages"] >= 2


async def test_get_view_uses_scope_question_id_when_omitted(tmp_db, seeded_question):
    question = seeded_question["question"]
    result_str = await _execute_tool(
        "get_view",
        {},
        tmp_db,
        scope_question_id=question.id,
    )
    payload = json.loads(result_str)
    assert payload["question_full_id"] == question.id


async def test_get_view_item_returns_full_content(tmp_db, seeded_question):
    question = seeded_question["question"]
    core_finding = seeded_question["core_finding"]

    result_str = await _execute_tool(
        "get_view_item",
        {"item_id": core_finding.id[:8], "question_id": question.id[:8]},
        tmp_db,
        scope_question_id=question.id,
    )
    payload = json.loads(result_str)

    assert payload["id"] == core_finding.id[:8]
    assert payload["full_id"] == core_finding.id
    assert payload["page_type"] == "claim"
    assert payload["headline"] == "Structured views beat reconstruction"
    assert payload["content"] == ("Models fetch structured views faster than reconstructing them.")
    assert payload["credence"] == 8
    assert payload["robustness"] == 4
    assert payload["importance"] == 1

    assert payload["section"] == "core_findings"
    assert payload["direction"] == "supports"

    assert isinstance(payload["outgoing_links"], list)
    assert isinstance(payload["incoming_links"], list)
    outgoing_types = {lk["link_type"] for lk in payload["outgoing_links"]}
    assert "consideration" in outgoing_types
    consideration_link = next(
        lk for lk in payload["outgoing_links"] if lk["link_type"] == "consideration"
    )
    assert consideration_link["to_id"] == question.id[:8]
    assert consideration_link["to_headline"] == question.headline


async def test_tools_list_exposes_view_tools():
    tool_names = {t["name"] for t in TOOLS}
    assert "get_view" in tool_names
    assert "get_view_item" in tool_names

    get_view_tool = next(t for t in TOOLS if t["name"] == "get_view")
    props = get_view_tool["input_schema"]["properties"]
    assert "question_id" in props
    assert "importance_threshold" in props
    assert "required" not in get_view_tool["input_schema"] or (
        "question_id" not in get_view_tool["input_schema"].get("required", [])
    )

    get_view_item_tool = next(t for t in TOOLS if t["name"] == "get_view_item")
    item_props = get_view_item_tool["input_schema"]["properties"]
    assert "item_id" in item_props
    assert "item_id" in get_view_item_tool["input_schema"]["required"]


async def test_get_view_nonexistent_question_returns_clean_error(tmp_db):
    result = await _execute_tool(
        "get_view",
        {"question_id": "deadbeef"},
        tmp_db,
        scope_question_id="",
    )
    assert "not found" in result.lower()
    with pytest.raises(json.JSONDecodeError):
        json.loads(result)


async def test_get_view_item_nonexistent_returns_clean_error(tmp_db, seeded_question):
    question = seeded_question["question"]
    result = await _execute_tool(
        "get_view_item",
        {"item_id": "deadbeef", "question_id": question.id[:8]},
        tmp_db,
        scope_question_id=question.id,
    )
    assert "not found" in result.lower()


async def test_get_view_rejects_non_question_page(tmp_db, seeded_question):
    """build_view raises ValueError for non-question pages; tool should return clean string."""
    core_finding = seeded_question["core_finding"]
    result = await _execute_tool(
        "get_view",
        {"question_id": core_finding.id[:8]},
        tmp_db,
        scope_question_id="",
    )
    assert "not a question" in result.lower() or "not found" in result.lower()


async def test_get_view_tool_dispatched_through_public_api(tmp_db, seeded_question):
    """Confirm the tool name is reachable through the module's dispatcher."""
    question = seeded_question["question"]
    result_str = await chat_module._execute_tool(
        "get_view",
        {"question_id": question.id[:8]},
        tmp_db,
        scope_question_id=question.id,
    )
    payload = json.loads(result_str)
    assert payload["question_full_id"] == question.id
