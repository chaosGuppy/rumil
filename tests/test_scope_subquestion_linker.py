"""Tests for the scope_subquestion_linker package."""

import pytest

from rumil.llm import AgentResult
from rumil.models import (
    CallStatus,
    CallType,
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)
from rumil.scope_subquestion_linker.runner import run_scope_subquestion_linker
from rumil.scope_subquestion_linker.seed_selection import select_seed_questions
from rumil.scope_subquestion_linker.subgraph import render_question_subgraph


def _make_question(headline: str, *, provenance_model: str = "human") -> Page:
    return Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=f"Content for {headline}",
        headline=headline,
        provenance_model=provenance_model,
    )


def _make_claim(headline: str) -> Page:
    return Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=f"Content for {headline}",
        headline=headline,
    )


async def _link_child(db, parent: Page, child: Page) -> None:
    await db.save_link(
        PageLink(
            from_page_id=parent.id,
            to_page_id=child.id,
            link_type=LinkType.CHILD_QUESTION,
        )
    )


async def test_render_question_subgraph_three_hops(tmp_db):
    q1 = _make_question("Q1 root")
    q2 = _make_question("Q2 child")
    q3 = _make_question("Q3 grandchild")
    q4 = _make_question("Q4 great-grandchild")
    q5 = _make_question("Q5 beyond horizon")
    for q in (q1, q2, q3, q4, q5):
        await tmp_db.save_page(q)
    await _link_child(tmp_db, q1, q2)
    await _link_child(tmp_db, q2, q3)
    await _link_child(tmp_db, q3, q4)
    await _link_child(tmp_db, q4, q5)

    rendered = await render_question_subgraph(q1.id, tmp_db, max_depth=3)

    assert q1.id[:8] in rendered
    assert q2.id[:8] in rendered
    assert q3.id[:8] in rendered
    assert q4.id[:8] in rendered
    assert q5.id[:8] not in rendered


async def test_render_question_subgraph_handles_cycles(tmp_db):
    q1 = _make_question("Q1")
    q2 = _make_question("Q2")
    await tmp_db.save_page(q1)
    await tmp_db.save_page(q2)
    await _link_child(tmp_db, q1, q2)
    await _link_child(tmp_db, q2, q1)

    rendered = await render_question_subgraph(q1.id, tmp_db)

    assert q1.id[:8] in rendered
    assert q2.id[:8] in rendered


async def test_render_question_subgraph_rejects_non_question(tmp_db):
    claim = _make_claim("Some claim")
    await tmp_db.save_page(claim)

    rendered = await render_question_subgraph(claim.id, tmp_db)

    assert "not a question" in rendered


async def test_render_question_subgraph_unknown_id(tmp_db):
    rendered = await render_question_subgraph("deadbeef", tmp_db)
    assert "not found" in rendered


async def test_select_seed_questions_skips_llm_when_few(tmp_db, mocker):
    scope = _make_question("Scope")
    await tmp_db.save_page(scope)
    others = [_make_question(f"Top {i}") for i in range(3)]
    for q in others:
        await tmp_db.save_page(q)

    spy = mocker.patch(
        "rumil.scope_subquestion_linker.seed_selection.structured_call",
        side_effect=AssertionError("structured_call should not be invoked"),
    )

    result = await select_seed_questions(scope, tmp_db, limit=10)

    assert spy.call_count == 0
    assert {p.id for p in result} == {q.id for q in others}


async def test_select_seed_questions_filters_non_human(tmp_db):
    scope = _make_question("Scope")
    await tmp_db.save_page(scope)
    human_q = _make_question("Human top", provenance_model="human")
    llm_q = _make_question("LLM top", provenance_model="claude-opus-4-6")
    await tmp_db.save_page(human_q)
    await tmp_db.save_page(llm_q)

    result = await select_seed_questions(scope, tmp_db, limit=10)

    ids = {p.id for p in result}
    assert human_q.id in ids
    assert llm_q.id not in ids
    assert scope.id not in ids


async def test_runner_filters_invalid_and_existing_children(tmp_db, mocker):
    scope = _make_question("Scope question")
    await tmp_db.save_page(scope)

    valid = _make_question("Valid candidate")
    await tmp_db.save_page(valid)

    existing_child = _make_question("Already linked")
    await tmp_db.save_page(existing_child)
    await _link_child(tmp_db, scope, existing_child)

    claim = _make_claim("Not a question")
    await tmp_db.save_page(claim)

    no_rationale = _make_question("Missing rationale")
    await tmp_db.save_page(no_rationale)

    agent_text = (
        "Here are my findings.\n\n"
        "```json\n"
        "{\n"
        f'  "linked_question_ids": ["{valid.id[:8]}", "{existing_child.id[:8]}", '
        f'"{scope.id[:8]}", "{claim.id[:8]}", "deadbeef", "{no_rationale.id[:8]}"],\n'
        '  "rationales": {\n'
        f'    "{valid.id[:8]}": "Direct influence path explained.",\n'
        f'    "{existing_child.id[:8]}": "Some rationale.",\n'
        f'    "{scope.id[:8]}": "Some rationale.",\n'
        f'    "{claim.id[:8]}": "Some rationale.",\n'
        '    "deadbeef": "Some rationale.",\n'
        f'    "{no_rationale.id[:8]}": ""\n'
        "  }\n"
        "}\n"
        "```\n"
    )

    async def fake_loop(*args, **kwargs):
        return AgentResult(text=agent_text)

    mocker.patch(
        "rumil.scope_subquestion_linker.runner.run_agent_loop",
        side_effect=fake_loop,
    )

    call = await run_scope_subquestion_linker(scope.id, tmp_db, max_rounds=1)

    assert call.status == CallStatus.COMPLETE
    assert call.call_type == CallType.LINK_SUBQUESTIONS
    assert call.review_json is not None
    assert call.review_json["proposed_subquestion_ids"] == [valid.id]
    assert call.review_json["rationales"] == {
        valid.id: "Direct influence path explained.",
    }


async def test_runner_handles_missing_json_block(tmp_db, mocker):
    scope = _make_question("Scope")
    await tmp_db.save_page(scope)

    async def fake_loop(*args, **kwargs):
        return AgentResult(text="No JSON block here, just prose.")

    mocker.patch(
        "rumil.scope_subquestion_linker.runner.run_agent_loop",
        side_effect=fake_loop,
    )

    call = await run_scope_subquestion_linker(scope.id, tmp_db, max_rounds=1)

    assert call.status == CallStatus.COMPLETE
    assert call.review_json == {
        "proposed_subquestion_ids": [],
        "rationales": {},
    }


async def test_runner_rejects_non_question_scope(tmp_db):
    claim = _make_claim("Not a question scope")
    await tmp_db.save_page(claim)

    with pytest.raises(ValueError, match="not a question"):
        await run_scope_subquestion_linker(claim.id, tmp_db)
