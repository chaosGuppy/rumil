"""Tests for the 12 new chat API tools added in api/chat.py.

All exercised through _execute_tool against the tmp_db fixture — no LLM
calls. Mutating tools are checked by reading the resulting DB state.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from rumil.api.chat import TOOLS, _aggregate_turn_research_cost, _execute_tool
from rumil.database import DB
from rumil.models import (
    Call,
    CallStatus,
    CallType,
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
async def seeded_graph(tmp_db):
    """A question with a child question, two considerations, and an incoming link."""
    await tmp_db.create_run(name="test-chat", question_id=None, config={})

    root = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Will tool-layer chat upgrades improve research velocity?",
        headline="Will tool-layer chat upgrades improve research velocity?",
    )
    await tmp_db.save_page(root)

    child = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="How much latency does one extra tool round add?",
        headline="How much latency does one extra tool round add?",
    )
    await tmp_db.save_page(child)
    await tmp_db.save_link(
        PageLink(
            from_page_id=root.id,
            to_page_id=child.id,
            link_type=LinkType.CHILD_QUESTION,
            reasoning="Latency budget",
            role=LinkRole.STRUCTURAL,
            impact_on_parent_question=6,
        )
    )

    strong = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Structured tools cut context reconstruction time by 3x.",
        headline="Structured tools cut reconstruction time",
        credence=7,
        robustness=3,
    )
    await tmp_db.save_page(strong)
    await tmp_db.save_link(
        PageLink(
            from_page_id=strong.id,
            to_page_id=root.id,
            link_type=LinkType.CONSIDERATION,
            strength=4.0,
            direction=ConsiderationDirection.SUPPORTS,
            role=LinkRole.DIRECT,
            reasoning="Measured on the A/B harness",
        )
    )

    weak = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="More tools may dilute the model's attention budget.",
        headline="Tool count may dilute attention",
        credence=4,
        robustness=2,
    )
    await tmp_db.save_page(weak)
    await tmp_db.save_link(
        PageLink(
            from_page_id=weak.id,
            to_page_id=root.id,
            link_type=LinkType.CONSIDERATION,
            strength=1.5,
            direction=ConsiderationDirection.OPPOSES,
            role=LinkRole.DIRECT,
            reasoning="Hypothesis, untested here",
        )
    )

    judgement = Page(
        page_type=PageType.JUDGEMENT,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="On balance, the upgrades help.",
        headline="Upgrades help on balance",
        credence=6,
        robustness=3,
    )
    await tmp_db.save_page(judgement)
    await tmp_db.save_link(
        PageLink(
            from_page_id=judgement.id,
            to_page_id=root.id,
            link_type=LinkType.ANSWERS,
        )
    )

    related = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Adjacent work on tool loaders.",
        headline="Adjacent tool-loader work",
        credence=5,
        robustness=2,
    )
    await tmp_db.save_page(related)
    await tmp_db.save_link(
        PageLink(
            from_page_id=related.id,
            to_page_id=strong.id,
            link_type=LinkType.RELATED,
            reasoning="Similar harness",
        )
    )

    return {
        "root": root,
        "child": child,
        "strong": strong,
        "weak": weak,
        "judgement": judgement,
        "related": related,
    }


async def test_all_new_tools_exposed_in_tools_list():
    names = {t["name"] for t in TOOLS}
    expected = {
        "get_considerations",
        "get_child_questions",
        "get_incoming_links",
        "get_parent_chain",
        "list_recent_calls",
        "get_call_trace",
        "create_claim",
        "create_judgement",
        "link_pages",
        "update_epistemic",
        "flag_page",
        "report_duplicate",
    }
    assert expected <= names


async def test_get_considerations_sorts_by_strength(tmp_db, seeded_graph):
    root = seeded_graph["root"]
    strong = seeded_graph["strong"]
    weak = seeded_graph["weak"]

    result = await _execute_tool(
        "get_considerations",
        {"question_id": root.id[:8]},
        tmp_db,
        scope_question_id=root.id,
    )

    assert strong.id[:8] in result
    assert weak.id[:8] in result
    assert "supports" in result and "opposes" in result
    assert "strength=4.0" in result and "strength=1.5" in result
    assert result.index(strong.id[:8]) < result.index(weak.id[:8])


async def test_get_considerations_uses_scope_when_omitted(tmp_db, seeded_graph):
    root = seeded_graph["root"]
    result = await _execute_tool(
        "get_considerations",
        {},
        tmp_db,
        scope_question_id=root.id,
    )
    assert seeded_graph["strong"].id[:8] in result


async def test_get_child_questions_shows_judgement_status(tmp_db, seeded_graph):
    root = seeded_graph["root"]
    child = seeded_graph["child"]

    result = await _execute_tool(
        "get_child_questions",
        {"question_id": root.id[:8]},
        tmp_db,
        scope_question_id=root.id,
    )

    assert child.id[:8] in result
    assert "no judgement" in result
    assert "impact=6" in result


async def test_get_incoming_links_lists_sources(tmp_db, seeded_graph):
    root = seeded_graph["root"]
    strong = seeded_graph["strong"]

    result = await _execute_tool(
        "get_incoming_links",
        {"short_id": root.id[:8]},
        tmp_db,
    )

    assert strong.id[:8] in result
    assert seeded_graph["weak"].id[:8] in result
    assert seeded_graph["judgement"].id[:8] in result
    assert "consideration" in result and "answers" in result


async def test_get_incoming_links_empty(tmp_db, seeded_graph):
    related = seeded_graph["related"]
    result = await _execute_tool(
        "get_incoming_links",
        {"short_id": related.id[:8]},
        tmp_db,
    )
    assert "No incoming links" in result


async def test_get_parent_chain_walks_up(tmp_db, seeded_graph):
    child = seeded_graph["child"]
    root = seeded_graph["root"]

    result = await _execute_tool(
        "get_parent_chain",
        {"short_id": child.id[:8]},
        tmp_db,
    )

    assert root.id[:8] in result


async def test_get_parent_chain_root_question(tmp_db, seeded_graph):
    root = seeded_graph["root"]
    result = await _execute_tool(
        "get_parent_chain",
        {"short_id": root.id[:8]},
        tmp_db,
    )
    assert "no parent chain" in result


async def test_list_recent_calls_returns_calls(tmp_db, seeded_graph):
    root = seeded_graph["root"]
    call = Call(
        call_type=CallType.FIND_CONSIDERATIONS,
        workspace=Workspace.RESEARCH,
        scope_page_id=root.id,
        status=CallStatus.COMPLETE,
        budget_allocated=3,
        budget_used=2,
        cost_usd=0.12,
        result_summary="Found three considerations on the root.",
    )
    await tmp_db.save_call(call)

    result = await _execute_tool(
        "list_recent_calls",
        {"question_id": root.id[:8]},
        tmp_db,
        scope_question_id=root.id,
    )

    assert call.id[:8] in result
    assert "find_considerations" in result
    assert "complete" in result
    assert "Found three considerations" in result


async def test_list_recent_calls_without_scope_errors(tmp_db, seeded_graph):
    result = await _execute_tool(
        "list_recent_calls",
        {},
        tmp_db,
    )
    assert "requires a question_id" in result


async def test_get_call_trace_dumps_events(tmp_db, seeded_graph):
    root = seeded_graph["root"]
    call = Call(
        call_type=CallType.ASSESS,
        workspace=Workspace.RESEARCH,
        scope_page_id=root.id,
        status=CallStatus.COMPLETE,
        budget_allocated=1,
        budget_used=1,
        cost_usd=0.05,
        result_summary="Graded two claims.",
    )
    await tmp_db.save_call(call)

    result = await _execute_tool(
        "get_call_trace",
        {"call_id": call.id[:8]},
        tmp_db,
    )

    assert call.id[:8] in result
    assert "assess" in result
    assert "Graded two claims" in result
    assert "trace event(s)" in result
    assert "LLM exchange(s)" in result


async def test_get_call_trace_unknown_call(tmp_db):
    result = await _execute_tool(
        "get_call_trace",
        {"call_id": "deadbeef"},
        tmp_db,
    )
    assert "not found" in result


async def test_create_claim_persists_page(tmp_db, seeded_graph):
    result = await _execute_tool(
        "create_claim",
        {
            "headline": "Prompt caching reduces per-turn latency materially",
            "content": "In profiling runs, cache hits dropped p50 latency 40%.",
            "credence": 7,
            "robustness": 3,
        },
        tmp_db,
    )

    assert "Created" in result
    all_pages = await tmp_db.get_pages()
    claims = [p for p in all_pages if p.page_type == PageType.CLAIM]
    assert any(p.headline == "Prompt caching reduces per-turn latency materially" for p in claims)


async def test_create_claim_links_as_consideration(tmp_db, seeded_graph):
    root = seeded_graph["root"]
    await _execute_tool(
        "create_claim",
        {
            "headline": "Batch embedding calls dominate per-request cost",
            "content": "Embeddings are ~60% of per-tool-round spend.",
            "question_id": root.id[:8],
            "strength": 3.5,
            "reasoning": "Cost attribution",
        },
        tmp_db,
    )
    pairs = await tmp_db.get_considerations_for_question(root.id)
    headlines = {claim.headline for claim, _ in pairs}
    assert "Batch embedding calls dominate per-request cost" in headlines
    matching = next(
        (link for claim, link in pairs if claim.headline.startswith("Batch embedding")),
        None,
    )
    assert matching is not None
    assert matching.strength == pytest.approx(3.5)
    assert matching.reasoning == "Cost attribution"


async def test_create_judgement_supersedes_prior(tmp_db, seeded_graph):
    root = seeded_graph["root"]
    old_judgement = seeded_graph["judgement"]

    await _execute_tool(
        "create_judgement",
        {
            "question_id": root.id[:8],
            "headline": "After more data, the upgrades help moderately",
            "content": "Evidence base updated; judgement revised.",
            "credence": 7,
            "robustness": 3,
            "key_dependencies": "Latency measurements stay flat",
            "sensitivity_analysis": "Would flip if p95 exceeds 2s",
        },
        tmp_db,
    )

    active = await tmp_db.get_judgements_for_question(root.id)
    active_headlines = {p.headline for p in active}
    assert "After more data, the upgrades help moderately" in active_headlines

    refreshed_old = await tmp_db.get_page(old_judgement.id)
    assert refreshed_old is not None
    assert refreshed_old.is_superseded


async def test_link_pages_related(tmp_db, seeded_graph):
    strong = seeded_graph["strong"]
    weak = seeded_graph["weak"]

    result = await _execute_tool(
        "link_pages",
        {
            "from_id": strong.id[:8],
            "to_id": weak.id[:8],
            "link_type": "related",
            "reasoning": "Both touch attention budget",
        },
        tmp_db,
    )

    assert "Done" in result or "linked" in result.lower()
    links = await tmp_db.get_links_from(strong.id)
    assert any(l.to_page_id == weak.id and l.link_type == LinkType.RELATED for l in links)


async def test_link_pages_child_question(tmp_db, seeded_graph):
    root = seeded_graph["root"]
    new_child = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Does prompt caching survive tool schema changes?",
        headline="Does prompt caching survive tool schema changes?",
    )
    await tmp_db.save_page(new_child)

    await _execute_tool(
        "link_pages",
        {
            "from_id": root.id[:8],
            "to_id": new_child.id[:8],
            "link_type": "child_question",
            "reasoning": "Cache stability sub-question",
        },
        tmp_db,
    )

    links = await tmp_db.get_links_from(root.id)
    assert any(
        l.to_page_id == new_child.id and l.link_type == LinkType.CHILD_QUESTION for l in links
    )


async def test_link_pages_consideration(tmp_db, seeded_graph):
    root = seeded_graph["root"]
    new_claim = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Users report faster iteration after the upgrade.",
        headline="Users report faster iteration after the upgrade",
        credence=6,
        robustness=2,
    )
    await tmp_db.save_page(new_claim)

    await _execute_tool(
        "link_pages",
        {
            "from_id": new_claim.id[:8],
            "to_id": root.id[:8],
            "link_type": "consideration",
            "strength": 3.0,
            "reasoning": "User survey",
        },
        tmp_db,
    )

    pairs = await tmp_db.get_considerations_for_question(root.id)
    assert any(claim.id == new_claim.id for claim, _ in pairs)


async def test_link_pages_unsupported_type_rejected(tmp_db, seeded_graph):
    strong = seeded_graph["strong"]
    weak = seeded_graph["weak"]
    result = await _execute_tool(
        "link_pages",
        {
            "from_id": strong.id[:8],
            "to_id": weak.id[:8],
            "link_type": "depends_on",
        },
        tmp_db,
    )
    assert "Unsupported link_type" in result


async def test_update_epistemic_applies_scores(tmp_db, seeded_graph):
    strong = seeded_graph["strong"]

    result = await _execute_tool(
        "update_epistemic",
        {
            "short_id": strong.id[:8],
            "credence": 9,
            "robustness": 5,
            "reasoning": "Replication run confirmed effect",
        },
        tmp_db,
    )

    assert "C9/R5" in result
    source, _ = await tmp_db.get_epistemic_score_source(strong.id)
    assert source is not None
    assert source["credence"] == 9
    assert source["robustness"] == 5


async def test_update_epistemic_rejects_question(tmp_db, seeded_graph):
    root = seeded_graph["root"]
    result = await _execute_tool(
        "update_epistemic",
        {
            "short_id": root.id[:8],
            "credence": 7,
            "robustness": 3,
            "reasoning": "n/a",
        },
        tmp_db,
    )
    assert "Cannot update epistemic" in result


async def _query_flags(tmp_db, flag_type: str) -> list[dict]:
    rows = (
        await tmp_db._execute(
            tmp_db.client.table("page_flags").select("*").eq("flag_type", flag_type)
        )
    ).data or []
    return rows


async def test_flag_page_records_flag(tmp_db, seeded_graph):
    weak = seeded_graph["weak"]

    result = await _execute_tool(
        "flag_page",
        {"short_id": weak.id[:8], "note": "Claim is speculative — no cited data"},
        tmp_db,
    )

    assert weak.id[:8] in result
    flags = await _query_flags(tmp_db, "funniness")
    matching = [f for f in flags if f.get("page_id") == weak.id]
    assert matching
    assert any("speculative" in (f.get("note") or "") for f in matching)


async def test_flag_page_unknown_id(tmp_db):
    result = await _execute_tool(
        "flag_page",
        {"short_id": "deadbeef", "note": "nope"},
        tmp_db,
    )
    assert "not found" in result


async def test_report_duplicate_records_flag(tmp_db, seeded_graph):
    strong = seeded_graph["strong"]
    weak = seeded_graph["weak"]

    result = await _execute_tool(
        "report_duplicate",
        {"page_id_a": strong.id[:8], "page_id_b": weak.id[:8]},
        tmp_db,
    )

    assert "Duplicate reported" in result
    flags = await _query_flags(tmp_db, "duplicate")
    assert any({f.get("page_id_a"), f.get("page_id_b")} == {strong.id, weak.id} for f in flags)


async def test_aggregate_turn_research_cost_sums_and_buckets(tmp_db, seeded_graph):

    root = seeded_graph["root"]
    turn_start = datetime.now(UTC) - timedelta(seconds=1)

    fc_call = Call(
        call_type=CallType.FIND_CONSIDERATIONS,
        workspace=Workspace.RESEARCH,
        scope_page_id=root.id,
        status=CallStatus.COMPLETE,
        cost_usd=0.25,
    )
    assess_call = Call(
        call_type=CallType.ASSESS,
        workspace=Workspace.RESEARCH,
        scope_page_id=root.id,
        status=CallStatus.COMPLETE,
        cost_usd=0.10,
    )
    chat_direct = Call(
        call_type=CallType.CHAT_DIRECT,
        workspace=Workspace.RESEARCH,
        scope_page_id=root.id,
        status=CallStatus.COMPLETE,
        cost_usd=0.001,
    )
    pending = Call(
        call_type=CallType.FIND_CONSIDERATIONS,
        workspace=Workspace.RESEARCH,
        scope_page_id=root.id,
        status=CallStatus.PENDING,
        cost_usd=None,
    )
    for c in (fc_call, assess_call, chat_direct, pending):
        await tmp_db.save_call(c)

    total, by_type = await _aggregate_turn_research_cost(tmp_db, turn_start.isoformat())

    assert total == pytest.approx(0.25 + 0.10 + 0.001)
    assert by_type["find_considerations"] == pytest.approx(0.25)
    assert by_type["assess"] == pytest.approx(0.10)
    assert by_type["chat_direct"] == pytest.approx(0.001)
    assert "pending_call" not in by_type


async def test_aggregate_turn_research_cost_ignores_calls_before_turn(tmp_db, seeded_graph):

    root = seeded_graph["root"]

    earlier = Call(
        call_type=CallType.FIND_CONSIDERATIONS,
        workspace=Workspace.RESEARCH,
        scope_page_id=root.id,
        status=CallStatus.COMPLETE,
        cost_usd=0.50,
    )
    await tmp_db.save_call(earlier)

    turn_start = datetime.now(UTC) + timedelta(seconds=1)
    total, by_type = await _aggregate_turn_research_cost(tmp_db, turn_start.isoformat())

    assert total == 0.0
    assert by_type == {}


async def test_aggregate_turn_research_cost_scoped_to_run_id(tmp_db, seeded_graph):

    root = seeded_graph["root"]
    turn_start = datetime.now(UTC) - timedelta(seconds=1)

    mine = Call(
        call_type=CallType.FIND_CONSIDERATIONS,
        workspace=Workspace.RESEARCH,
        scope_page_id=root.id,
        status=CallStatus.COMPLETE,
        cost_usd=0.30,
    )
    await tmp_db.save_call(mine)

    other = await DB.create(run_id="11111111-1111-1111-1111-111111111111")
    other.project_id = tmp_db.project_id
    try:
        await other.create_run(name="other", question_id=None, config={})
        stranger = Call(
            call_type=CallType.FIND_CONSIDERATIONS,
            workspace=Workspace.RESEARCH,
            scope_page_id=root.id,
            status=CallStatus.COMPLETE,
            cost_usd=99.99,
        )
        await other.save_call(stranger)
    finally:
        await other.delete_run_data(delete_project=False)
        await other.close()

    total, by_type = await _aggregate_turn_research_cost(tmp_db, turn_start.isoformat())
    assert total == pytest.approx(0.30)
    assert by_type["find_considerations"] == pytest.approx(0.30)


async def _set_run_config(tmp_db, config: dict) -> None:
    await tmp_db._execute(
        tmp_db.client.table("runs").update({"config": config}).eq("id", tmp_db.run_id)
    )


async def test_get_run_returns_orchestrator(tmp_db, seeded_graph):
    root = seeded_graph["root"]
    await _set_run_config(
        tmp_db,
        {"orchestrator": "two_phase", "model": "claude-sonnet", "origin": "cli"},
    )

    fc = Call(
        call_type=CallType.FIND_CONSIDERATIONS,
        workspace=Workspace.RESEARCH,
        scope_page_id=root.id,
        status=CallStatus.COMPLETE,
        budget_used=1,
        cost_usd=0.20,
    )
    assess = Call(
        call_type=CallType.ASSESS,
        workspace=Workspace.RESEARCH,
        scope_page_id=root.id,
        status=CallStatus.COMPLETE,
        budget_used=1,
        cost_usd=0.05,
    )
    for c in (fc, assess):
        await tmp_db.save_call(c)

    result = await _execute_tool("get_run", {"run_id": tmp_db.run_id}, tmp_db)

    assert "two_phase" in result
    assert "claude-sonnet" in result
    assert "find_considerations=1" in result
    assert "assess=1" in result
    assert "calls: 2" in result
    assert "total cost: $0.250" in result


async def test_get_run_resolves_short_id(tmp_db, seeded_graph):
    await _set_run_config(tmp_db, {"orchestrator": "two_phase"})
    short = tmp_db.run_id[:8]
    result = await _execute_tool("get_run", {"run_id": short}, tmp_db)
    assert "two_phase" in result
    assert short in result


async def test_get_run_not_found(tmp_db, seeded_graph):
    result = await _execute_tool(
        "get_run",
        {"run_id": "00000000-0000-0000-0000-000000000000"},
        tmp_db,
    )
    assert "not found" in result


async def test_get_call_trace_includes_run_origin(tmp_db, seeded_graph):
    root = seeded_graph["root"]
    await _set_run_config(tmp_db, {"orchestrator": "claim_investigation"})

    call = Call(
        call_type=CallType.ASSESS,
        workspace=Workspace.RESEARCH,
        scope_page_id=root.id,
        status=CallStatus.COMPLETE,
        budget_used=1,
        cost_usd=0.02,
        result_summary="Graded one claim.",
    )
    await tmp_db.save_call(call)

    result = await _execute_tool("get_call_trace", {"call_id": call.id[:8]}, tmp_db)

    assert "orchestrator:" in result
    assert "claim_investigation" in result
    assert tmp_db.run_id[:8] in result


async def test_list_recent_calls_includes_orch(tmp_db, seeded_graph):
    root = seeded_graph["root"]
    await _set_run_config(tmp_db, {"orchestrator": "two_phase"})

    a = Call(
        call_type=CallType.FIND_CONSIDERATIONS,
        workspace=Workspace.RESEARCH,
        scope_page_id=root.id,
        status=CallStatus.COMPLETE,
        budget_used=1,
        cost_usd=0.10,
    )
    b = Call(
        call_type=CallType.ASSESS,
        workspace=Workspace.RESEARCH,
        scope_page_id=root.id,
        status=CallStatus.COMPLETE,
        budget_used=1,
        cost_usd=0.05,
    )
    for c in (a, b):
        await tmp_db.save_call(c)

    result = await _execute_tool(
        "list_recent_calls",
        {"question_id": root.id[:8]},
        tmp_db,
        scope_question_id=root.id,
    )

    assert "orch=two_phase" in result
    assert result.count("orch=two_phase") >= 2


async def test_build_chat_context_tags_run_ids(tmp_db, seeded_graph):
    from rumil.api.chat import build_chat_context

    root = seeded_graph["root"]
    strong = seeded_graph["strong"]

    result = await build_chat_context(root.id, tmp_db)

    assert f"run={tmp_db.run_id[:8]}" in result
    assert strong.id[:8] in result


async def test_chat_request_accepts_open_run_id():
    from rumil.api.chat import ChatRequest

    req = ChatRequest(
        question_id="abc",
        messages=[{"role": "user", "content": "hi"}],
        open_run_id="780ffdf0",
        open_page_ids=["77d295eb", "d19b6c91"],
    )
    assert req.open_run_id == "780ffdf0"
    assert req.open_page_ids == ["77d295eb", "d19b6c91"]

    req2 = ChatRequest(question_id="abc", messages=[])
    assert req2.open_run_id is None
    assert req2.open_page_ids == []


async def test_build_ui_state_block_renders_run_and_pages(tmp_db, seeded_graph):
    from rumil.api.chat import _build_ui_state_block

    await _set_run_config(tmp_db, {"orchestrator": "two_phase"})
    strong = seeded_graph["strong"]

    block = await _build_ui_state_block(
        tmp_db,
        tmp_db.run_id[:8],
        [strong.id[:8]],
    )

    assert "Currently open in UI" in block
    assert tmp_db.run_id[:8] in block
    assert "two_phase" in block
    assert strong.id[:8] in block


async def test_build_ui_state_block_empty_when_no_inputs(tmp_db):
    from rumil.api.chat import _build_ui_state_block

    block = await _build_ui_state_block(tmp_db, None, [])
    assert block == ""


async def test_build_ui_state_block_handles_missing_run_gracefully(tmp_db, seeded_graph):
    from rumil.api.chat import _build_ui_state_block

    block = await _build_ui_state_block(tmp_db, "deadbeef", [])
    assert block == ""


async def test_build_research_tree_shows_run_ids(tmp_db, seeded_graph):
    from rumil.summary import build_research_tree

    root = seeded_graph["root"]

    off = await build_research_tree(root.id, tmp_db, max_depth=2, show_run_ids=False)
    assert f"run={tmp_db.run_id[:8]}" not in off

    on = await build_research_tree(root.id, tmp_db, max_depth=2, show_run_ids=True)
    assert f"run={tmp_db.run_id[:8]}" in on


async def test_get_recent_activity_tool_registered():
    names = {t["name"] for t in TOOLS}
    assert "get_recent_activity" in names


async def test_get_recent_activity_happy_path(tmp_db, seeded_graph):
    import json

    root = seeded_graph["root"]
    await tmp_db._execute(
        tmp_db.client.table("runs")
        .update({"config": {"orchestrator": "two_phase"}, "question_id": root.id})
        .eq("id", tmp_db.run_id)
    )

    call = Call(
        call_type=CallType.FIND_CONSIDERATIONS,
        workspace=Workspace.RESEARCH,
        scope_page_id=root.id,
        status=CallStatus.COMPLETE,
        cost_usd=0.42,
    )
    await tmp_db.save_call(call)

    result = await _execute_tool("get_recent_activity", {}, tmp_db)
    payload = json.loads(result)

    assert payload["window_hours"] == 24
    assert payload["scope_question_id"] is None

    run_ids = {r["run_id"] for r in payload["recent_runs"]}
    assert tmp_db.run_id[:8] in run_ids
    matching_run = next(r for r in payload["recent_runs"] if r["run_id"] == tmp_db.run_id[:8])
    assert matching_run["orchestrator"] == "two_phase"
    assert matching_run["cost_usd"] == pytest.approx(0.42)
    assert matching_run["question_summary"] == root.headline
    assert matching_run["staged"] is False

    page_ids = {p["page_id"] for p in payload["recent_pages"]}
    assert root.id[:8] in page_ids
    assert seeded_graph["strong"].id[:8] in page_ids

    dispatch_ids = {d["call_id"] for d in payload["recent_dispatches"]}
    assert call.id[:8] in dispatch_ids
    matching_dispatch = next(d for d in payload["recent_dispatches"] if d["call_id"] == call.id[:8])
    assert matching_dispatch["call_type"] == "find_considerations"
    assert matching_dispatch["scope_page_id"] == root.id[:8]


async def test_get_recent_activity_question_scope_filters(tmp_db, seeded_graph):
    import json
    import uuid

    root = seeded_graph["root"]
    await tmp_db._execute(
        tmp_db.client.table("runs")
        .update({"question_id": root.id, "config": {"orchestrator": "two_phase"}})
        .eq("id", tmp_db.run_id)
    )

    other_root = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        headline="Unrelated question about foo",
        content="Unrelated question about foo",
    )
    await tmp_db.save_page(other_root)

    other_run_id = str(uuid.uuid4())
    other_db = await DB.create(run_id=other_run_id)
    other_db.project_id = tmp_db.project_id
    try:
        await other_db.create_run(
            name="other", question_id=other_root.id, config={"orchestrator": "claim_investigation"}
        )
        other_page = Page(
            page_type=PageType.CLAIM,
            layer=PageLayer.SQUIDGY,
            workspace=Workspace.RESEARCH,
            headline="Claim from unrelated run",
            content="Claim from unrelated run",
            run_id=other_run_id,
        )
        await other_db.save_page(other_page)
        other_call = Call(
            call_type=CallType.ASSESS,
            workspace=Workspace.RESEARCH,
            scope_page_id=other_root.id,
            status=CallStatus.COMPLETE,
            cost_usd=0.01,
        )
        await other_db.save_call(other_call)

        result = await _execute_tool(
            "get_recent_activity",
            {"question_id": root.id[:8]},
            tmp_db,
        )
        payload = json.loads(result)

        assert payload["scope_question_id"] == root.id[:8]

        run_ids = {r["run_id"] for r in payload["recent_runs"]}
        assert tmp_db.run_id[:8] in run_ids
        assert other_run_id[:8] not in run_ids

        page_ids = {p["page_id"] for p in payload["recent_pages"]}
        assert other_page.id[:8] not in page_ids

        dispatch_ids = {d["call_id"] for d in payload["recent_dispatches"]}
        assert other_call.id[:8] not in dispatch_ids
    finally:
        await other_db.delete_run_data(delete_project=False)
        await other_db.close()


async def test_get_recent_activity_time_window_excludes_old(tmp_db, seeded_graph):
    import json

    root = seeded_graph["root"]
    old_ts = (datetime.now(UTC) - timedelta(hours=48)).isoformat()
    await tmp_db._execute(
        tmp_db.client.table("runs")
        .update({"created_at": old_ts, "config": {"orchestrator": "two_phase"}})
        .eq("id", tmp_db.run_id)
    )

    old_call = Call(
        call_type=CallType.FIND_CONSIDERATIONS,
        workspace=Workspace.RESEARCH,
        scope_page_id=root.id,
        status=CallStatus.COMPLETE,
        cost_usd=0.10,
    )
    await tmp_db.save_call(old_call)
    await tmp_db._execute(
        tmp_db.client.table("calls").update({"created_at": old_ts}).eq("id", old_call.id)
    )

    await tmp_db._execute(
        tmp_db.client.table("pages").update({"created_at": old_ts}).eq("id", root.id)
    )

    result = await _execute_tool("get_recent_activity", {"hours": 24}, tmp_db)
    payload = json.loads(result)

    run_ids = {r["run_id"] for r in payload["recent_runs"]}
    assert tmp_db.run_id[:8] not in run_ids

    dispatch_ids = {d["call_id"] for d in payload["recent_dispatches"]}
    assert old_call.id[:8] not in dispatch_ids

    page_ids = {p["page_id"] for p in payload["recent_pages"]}
    assert root.id[:8] not in page_ids


async def test_get_recent_activity_respects_limit(tmp_db, seeded_graph):
    import json

    root = seeded_graph["root"]

    for _ in range(8):
        c = Call(
            call_type=CallType.FIND_CONSIDERATIONS,
            workspace=Workspace.RESEARCH,
            scope_page_id=root.id,
            status=CallStatus.COMPLETE,
            cost_usd=0.01,
        )
        await tmp_db.save_call(c)

    result = await _execute_tool("get_recent_activity", {"limit": 3}, tmp_db)
    payload = json.loads(result)

    assert len(payload["recent_dispatches"]) <= 3
    assert len(payload["recent_pages"]) <= 3
    assert len(payload["recent_runs"]) <= 3


async def test_get_recent_activity_unknown_question(tmp_db):
    result = await _execute_tool(
        "get_recent_activity",
        {"question_id": "deadbeef"},
        tmp_db,
    )
    assert "not found" in result


async def test_set_view_tool_registered():
    names = {t["name"] for t in TOOLS}
    assert "set_view" in names


@pytest.mark.parametrize(
    "view",
    ("panes", "article", "vertical", "sections", "sources"),
)
async def test_set_view_happy_path_non_trace(tmp_db, view):
    import json

    result = await _execute_tool("set_view", {"view": view}, tmp_db)
    payload = json.loads(result)

    assert "__navigate__" in payload
    assert payload["__navigate__"]["view"] == view
    assert "message" in payload
    assert view in payload["message"]


async def test_set_view_trace_requires_run_id(tmp_db):
    import json

    result = await _execute_tool("set_view", {"view": "trace"}, tmp_db)
    payload = json.loads(result)

    assert "error" in payload
    assert "run_id" in payload["error"]
    assert "__navigate__" not in payload


async def test_set_view_trace_resolves_short_run_id(tmp_db, seeded_graph):
    import json

    result = await _execute_tool(
        "set_view",
        {"view": "trace", "run_id": tmp_db.run_id[:8]},
        tmp_db,
    )
    payload = json.loads(result)

    assert payload["__navigate__"]["view"] == "trace"
    assert payload["__navigate__"]["run_id"] == tmp_db.run_id
    assert payload["__navigate__"]["run_id_short"] == tmp_db.run_id[:8]
    assert tmp_db.run_id[:8] in payload["message"]


async def test_set_view_trace_accepts_full_run_id(tmp_db, seeded_graph):
    import json

    result = await _execute_tool(
        "set_view",
        {"view": "trace", "run_id": tmp_db.run_id},
        tmp_db,
    )
    payload = json.loads(result)

    assert payload["__navigate__"]["run_id"] == tmp_db.run_id


async def test_set_view_unknown_run_id_returns_error(tmp_db):
    import json

    result = await _execute_tool(
        "set_view",
        {"view": "trace", "run_id": "deadbeef"},
        tmp_db,
    )
    payload = json.loads(result)

    assert "error" in payload
    assert "not found" in payload["error"]


async def test_set_view_resolves_call_id(tmp_db, seeded_graph):
    import json

    root = seeded_graph["root"]
    call = Call(
        call_type=CallType.ASSESS,
        workspace=Workspace.RESEARCH,
        scope_page_id=root.id,
        status=CallStatus.COMPLETE,
        cost_usd=0.02,
    )
    await tmp_db.save_call(call)

    result = await _execute_tool(
        "set_view",
        {
            "view": "trace",
            "run_id": tmp_db.run_id[:8],
            "call_id": call.id[:8],
        },
        tmp_db,
    )
    payload = json.loads(result)

    assert payload["__navigate__"]["call_id"] == call.id
    assert payload["__navigate__"]["call_id_short"] == call.id[:8]


async def test_set_view_unknown_call_id_returns_error(tmp_db, seeded_graph):
    import json

    result = await _execute_tool(
        "set_view",
        {
            "view": "trace",
            "run_id": tmp_db.run_id[:8],
            "call_id": "deadbeef",
        },
        tmp_db,
    )
    payload = json.loads(result)

    assert "error" in payload
    assert "not found" in payload["error"]


async def test_set_view_resolves_question_id(tmp_db, seeded_graph):
    import json

    root = seeded_graph["root"]

    result = await _execute_tool(
        "set_view",
        {"view": "panes", "question_id": root.id[:8]},
        tmp_db,
    )
    payload = json.loads(result)

    assert payload["__navigate__"]["question_id"] == root.id
    assert payload["__navigate__"]["question_id_short"] == root.id[:8]


async def test_set_view_normalizes_panes(tmp_db, seeded_graph):
    import json

    strong = seeded_graph["strong"]
    weak = seeded_graph["weak"]

    result = await _execute_tool(
        "set_view",
        {
            "view": "panes",
            "panes": [strong.id[:8].upper(), f"  {weak.id[:8]}  "],
        },
        tmp_db,
    )
    payload = json.loads(result)

    assert payload["__navigate__"]["panes"] == [strong.id[:8], weak.id[:8]]


async def test_set_view_rejects_invalid_pane(tmp_db):
    import json

    result = await _execute_tool(
        "set_view",
        {"view": "panes", "panes": ["notahex!"]},
        tmp_db,
    )
    payload = json.loads(result)

    assert "error" in payload
    assert "Invalid pane" in payload["error"]


async def test_set_view_invalid_view_mode(tmp_db):
    import json

    result = await _execute_tool("set_view", {"view": "nonsense"}, tmp_db)
    payload = json.loads(result)

    assert "error" in payload
    assert "Invalid view" in payload["error"]


async def test_dispatch_call_tool_schema_includes_model_enum():
    dispatch_tool = next(t for t in TOOLS if t["name"] == "dispatch_call")
    props = dispatch_tool["input_schema"]["properties"]
    assert "model" in props
    assert props["model"]["type"] == "string"
    assert props["model"]["enum"] == ["haiku", "sonnet", "opus"]


async def test_run_dispatch_applies_model_override(tmp_db, seeded_graph, mocker):
    from rumil.api.chat import _run_dispatch
    from rumil.settings import get_settings

    root = seeded_graph["root"]
    captured: dict[str, str] = {}

    async def fake_run(self):
        captured["model"] = get_settings().model

    mocker.patch("rumil.calls.FindConsiderationsCall.run", fake_run)

    result = await _run_dispatch(
        tmp_db,
        {
            "question_id": root.id,
            "headline": root.headline,
            "call_type": "find-considerations",
            "max_rounds": 1,
            "model": "claude-opus-4-6",
        },
    )

    assert "completed" in result
    assert captured["model"] == "claude-opus-4-6"


async def test_run_dispatch_without_model_uses_default(tmp_db, seeded_graph, mocker):
    from rumil.api.chat import _run_dispatch
    from rumil.settings import get_settings

    root = seeded_graph["root"]
    default_model = get_settings().model
    captured: dict[str, str] = {}

    async def fake_run(self):
        captured["model"] = get_settings().model

    mocker.patch("rumil.calls.FindConsiderationsCall.run", fake_run)

    await _run_dispatch(
        tmp_db,
        {
            "question_id": root.id,
            "headline": root.headline,
            "call_type": "find-considerations",
            "max_rounds": 1,
            "model": None,
        },
    )

    assert captured["model"] == default_model


async def test_execute_tool_dispatch_envelope_forwards_validated_model(tmp_db, seeded_graph):
    import json

    from rumil.api.chat import MODEL_MAP

    root = seeded_graph["root"]

    result = await _execute_tool(
        "dispatch_call",
        {
            "question_id": root.id[:8],
            "call_type": "find-considerations",
            "model": "haiku",
        },
        tmp_db,
        scope_question_id=root.id,
    )
    payload = json.loads(result)

    assert payload["__async_dispatch__"] is True
    assert payload["model"] == MODEL_MAP["haiku"]


async def test_execute_tool_dispatch_envelope_drops_unknown_model(tmp_db, seeded_graph):
    import json

    root = seeded_graph["root"]

    result = await _execute_tool(
        "dispatch_call",
        {
            "question_id": root.id[:8],
            "call_type": "find-considerations",
            "model": "bogus",
        },
        tmp_db,
        scope_question_id=root.id,
    )
    payload = json.loads(result)

    assert payload["__async_dispatch__"] is True
    assert payload["model"] is None
