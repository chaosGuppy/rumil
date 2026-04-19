"""Tests for the 12 new chat API tools added in api/chat.py.

All exercised through _execute_tool against the tmp_db fixture — no LLM
calls. Mutating tools are checked by reading the resulting DB state.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from rumil.api.chat import TOOLS, _execute_tool
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
