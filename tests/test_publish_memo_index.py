"""Tests for publish_memo_index — the helper that lands the memo summary
as a SUMMARY page linked to the root question and to each drafted memo."""

from collections.abc import Sequence

import pytest

from rumil.memos import MemoCandidate, MemoScan
from rumil.memos_to_artefacts import publish_memo_index
from rumil.models import LinkType, Page, PageLayer, PageType, Workspace
from rumil.orchestrators.generative import GenerativeResult


def _make_candidate(title: str) -> MemoCandidate:
    return MemoCandidate(
        title=title,
        headline_claim=f"{title} headline claim",
        content_guess=f"{title} content sketch",
        importance=4,
        surprise=3,
        why_important="load-bearing",
        why_surprising="counters a common prior",
        relevant_page_ids=[],
        epistemic_signals="rests on a single Fermi estimate",
    )


async def _make_artefact(tmp_db, headline: str) -> Page:
    page = Page(
        page_type=PageType.ARTEFACT,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Memo body.",
        headline=headline,
    )
    await tmp_db.save_page(page)
    return page


@pytest.fixture
async def memo_scan(question_page) -> MemoScan:
    return MemoScan(
        scan_notes="",
        candidates=[_make_candidate("Alpha"), _make_candidate("Beta")],
        excluded=[],
        root_question_id=question_page.id,
        root_question_headline=question_page.headline,
    )


async def test_publish_creates_summary_page_and_links_to_question_and_memos(
    tmp_db, question_page, memo_scan
):
    artefact_a = await _make_artefact(tmp_db, "Memo: Alpha")
    artefact_b = await _make_artefact(tmp_db, "Memo: Beta")
    drafted: Sequence = [
        (
            memo_scan.candidates[0],
            GenerativeResult(task_id="t1", artefact_id=artefact_a.id, finalized=True),
            None,
        ),
        (
            memo_scan.candidates[1],
            GenerativeResult(task_id="t2", artefact_id=artefact_b.id, finalized=True),
            None,
        ),
    ]
    summary_text = "## Memo: Alpha\n\nAlpha paragraph.\n\n## Memo: Beta\n\nBeta paragraph."

    index_id = await publish_memo_index(summary_text, memo_scan, drafted, tmp_db)

    assert index_id is not None
    index_page = await tmp_db.get_page(index_id)
    assert index_page is not None
    assert index_page.page_type is PageType.SUMMARY
    assert summary_text.strip() in index_page.content
    assert question_page.headline[:40] in index_page.headline

    out_links = await tmp_db.get_links_from(index_id)
    summarizes = [link for link in out_links if link.link_type is LinkType.SUMMARIZES]
    related = [link for link in out_links if link.link_type is LinkType.RELATED]

    assert len(summarizes) == 1
    assert summarizes[0].to_page_id == question_page.id

    related_targets = {link.to_page_id for link in related}
    assert related_targets == {artefact_a.id, artefact_b.id}


async def test_publish_returns_none_when_no_drafted_memos(tmp_db, memo_scan):
    drafted: Sequence = [
        (
            memo_scan.candidates[0],
            GenerativeResult(task_id="t1", artefact_id=None, finalized=False),
            None,
        ),
    ]
    index_id = await publish_memo_index("(unused)", memo_scan, drafted, tmp_db)
    assert index_id is None


async def test_publish_skips_failed_drafts_in_links(tmp_db, question_page, memo_scan):
    artefact_a = await _make_artefact(tmp_db, "Memo: Alpha")
    drafted: Sequence = [
        (
            memo_scan.candidates[0],
            GenerativeResult(task_id="t1", artefact_id=artefact_a.id, finalized=True),
            None,
        ),
        (
            memo_scan.candidates[1],
            GenerativeResult(task_id="t2", artefact_id=None, finalized=False),
            None,
        ),
    ]
    index_id = await publish_memo_index("body", memo_scan, drafted, tmp_db)

    assert index_id is not None
    out_links = await tmp_db.get_links_from(index_id)
    related = [link for link in out_links if link.link_type is LinkType.RELATED]
    assert {link.to_page_id for link in related} == {artefact_a.id}
