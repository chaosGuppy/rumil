"""Tests for rumil_skills.scan — structural/distributional health checks.

All tests exercise pure heuristic code paths. No LLM calls.
"""

from __future__ import annotations

import pytest
from rumil_skills import _runctx, scan

from rumil.models import (
    ConsiderationDirection,
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    monkeypatch.setattr(_runctx, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(_runctx, "STATE_FILE", tmp_path / "state" / "rumil-session.json")


async def _make_claim(
    db,
    headline: str,
    *,
    credence: int | None = None,
    robustness: int | None = None,
) -> Page:
    page = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=f"Body of {headline}",
        headline=headline,
        credence=credence,
        robustness=robustness,
    )
    await db.save_page(page)
    return page


async def _make_question(db, headline: str) -> Page:
    page = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=f"Body of {headline}",
        headline=headline,
    )
    await db.save_page(page)
    return page


async def _link_consideration(
    db,
    claim_id: str,
    question_id: str,
    *,
    direction: ConsiderationDirection = ConsiderationDirection.SUPPORTS,
) -> None:
    await db.save_link(
        PageLink(
            from_page_id=claim_id,
            to_page_id=question_id,
            link_type=LinkType.CONSIDERATION,
            direction=direction,
        )
    )


async def _link_child_question(db, parent_id: str, child_id: str) -> None:
    await db.save_link(
        PageLink(
            from_page_id=parent_id,
            to_page_id=child_id,
            link_type=LinkType.CHILD_QUESTION,
        )
    )


async def test_collect_subtree_pulls_root_and_linked_claims(tmp_db, question_page):
    claim = await _make_claim(tmp_db, "Some relevant claim")
    await _link_consideration(tmp_db, claim.id, question_page.id)

    data = await scan.collect_subtree(tmp_db, question_page.id)

    assert data.root_id == question_page.id
    assert question_page.id in data.pages
    assert claim.id in data.pages
    assert len(data.questions) == 1
    assert len(data.claims) == 1


async def test_collect_subtree_walks_child_questions(tmp_db, question_page):
    child = await _make_question(tmp_db, "A child question")
    grandchild = await _make_question(tmp_db, "A grandchild question")
    await _link_child_question(tmp_db, question_page.id, child.id)
    await _link_child_question(tmp_db, child.id, grandchild.id)

    data = await scan.collect_subtree(tmp_db, question_page.id)

    assert child.id in data.pages
    assert grandchild.id in data.pages
    assert len(data.questions) == 3


async def test_graph_health_flags_barren_child_question(tmp_db, question_page):
    child = await _make_question(tmp_db, "A barren subquestion")
    await _link_child_question(tmp_db, question_page.id, child.id)

    data = await scan.collect_subtree(tmp_db, question_page.id)
    findings = scan.graph_health(data)

    codes = [f.code for f in findings]
    assert "barren_question" in codes
    barren = next(f for f in findings if f.code == "barren_question")
    assert child.id in barren.page_ids


async def test_graph_health_skips_root_barren(tmp_db, question_page):
    """Root with 0 cons is fine — only child questions trigger barren_question."""
    data = await scan.collect_subtree(tmp_db, question_page.id)
    findings = scan.graph_health(data)
    codes = [f.code for f in findings]
    assert "barren_question" not in codes


async def test_graph_health_flags_unjudged_question(tmp_db, question_page):
    for i in range(3):
        claim = await _make_claim(tmp_db, f"Claim {i}")
        await _link_consideration(tmp_db, claim.id, question_page.id)

    data = await scan.collect_subtree(tmp_db, question_page.id)
    findings = scan.graph_health(data)

    assert any(f.code == "unjudged_question" for f in findings)


async def test_rating_shape_flags_no_ratings(tmp_db, question_page):
    for i in range(2):
        claim = await _make_claim(tmp_db, f"Unrated claim {i}")
        await _link_consideration(tmp_db, claim.id, question_page.id)

    data = await scan.collect_subtree(tmp_db, question_page.id)
    findings = scan.rating_shape(data)

    assert any(f.code == "no_ratings" for f in findings)


async def test_rating_shape_produces_summary_for_rated_claims(tmp_db, question_page):
    for i, (c, r) in enumerate([(7, 1), (3, 4), (8, 2), (4, 3), (6, 5)]):
        claim = await _make_claim(
            tmp_db,
            f"Rated claim {i}",
            credence=c,
            robustness=r,
        )
        await _link_consideration(tmp_db, claim.id, question_page.id)

    data = await scan.collect_subtree(tmp_db, question_page.id)
    findings = scan.rating_shape(data)

    summaries = [f for f in findings if f.code == "summary"]
    assert len(summaries) == 1
    assert "rated claims" in summaries[0].description


async def test_rating_shape_flags_direction_imbalance(tmp_db, question_page):
    for i in range(4):
        claim = await _make_claim(
            tmp_db,
            f"Pro claim {i}",
            credence=5,
            robustness=3,
        )
        await _link_consideration(
            tmp_db,
            claim.id,
            question_page.id,
            direction=ConsiderationDirection.SUPPORTS,
        )

    data = await scan.collect_subtree(tmp_db, question_page.id)
    findings = scan.rating_shape(data)

    assert any(f.code == "direction_imbalance" for f in findings)


async def test_review_signals_empty_when_no_calls(tmp_db, question_page):
    data = await scan.collect_subtree(tmp_db, question_page.id)
    findings = await scan.review_signals(tmp_db, data)
    assert findings == []


async def test_review_signals_flags_inadequate_context(tmp_db, question_page, scout_call):
    scout_call.status = scout_call.status.__class__.COMPLETE
    scout_call.review_json = {
        "context_was_adequate": False,
        "what_was_missing": "the prior work on X",
    }
    await tmp_db.save_call(scout_call)

    data = await scan.collect_subtree(tmp_db, question_page.id)
    findings = await scan.review_signals(tmp_db, data)

    codes = [f.code for f in findings]
    assert "inadequate_context" in codes
    assert "what_was_missing" in codes


async def test_scan_all_combines_findings(tmp_db, question_page):
    child = await _make_question(tmp_db, "Barren subq")
    await _link_child_question(tmp_db, question_page.id, child.id)

    data, findings = await scan.scan_all(tmp_db, question_page.id)

    assert data.root_id == question_page.id
    codes = {f.code for f in findings}
    assert "barren_question" in codes


def test_format_findings_empty():
    assert scan.format_findings([]) == "(no findings)"


def test_format_findings_includes_code_and_severity():
    findings = [
        scan.Finding(
            category="graph_health",
            severity=3,
            code="barren_question",
            description="q1 has 0 considerations",
            page_ids=["abcdef1234"],
            suggested_action="dispatch find_considerations",
        ),
    ]
    out = scan.format_findings(findings)
    assert "barren_question" in out
    assert "[3]" in out
    assert "dispatch find_considerations" in out


def test_format_compact_reports_severity_counts():
    findings = [
        scan.Finding(
            category="graph_health",
            severity=3,
            code="barren_question",
            description="x",
        ),
        scan.Finding(
            category="graph_health",
            severity=3,
            code="dead_end_decomposition",
            description="x",
        ),
        scan.Finding(
            category="rating_shape",
            severity=0,
            code="summary",
            description="10 rated claims, mean C5.0/R3.0",
        ),
    ]
    compact = scan.format_compact(findings)
    assert "2x s3" in compact
    assert "rated claims" in compact


def test_format_compact_no_data():
    assert scan.format_compact([]) == "(no data)"
