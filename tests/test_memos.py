"""Tests for memo-scanner: schema, persistence, rendering, scan_for_memos
preconditions, end-to-end LLM call, and CLI wiring.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from collections.abc import Sequence
from pathlib import Path

import pytest
from pydantic import ValidationError

import rumil.memos as memos
from rumil.memos import (
    ExcludedFinding,
    MemoCandidate,
    MemoScan,
    render_scan_summary,
    save_memo_scan,
    scan_for_memos,
)
from rumil.models import (
    ConsiderationDirection,
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)


def _candidate(
    *,
    title: str = "Default",
    headline_claim: str = "A headline.",
    content_guess: str = "A guess.",
    importance: int = 3,
    surprise: int = 3,
    why_important: str = "It is.",
    why_surprising: str = "It is.",
    relevant_page_ids: Sequence[str] = ("aaaaaaaa",),
    epistemic_signals: str = "",
) -> MemoCandidate:
    return MemoCandidate(
        title=title,
        headline_claim=headline_claim,
        content_guess=content_guess,
        importance=importance,
        surprise=surprise,
        why_important=why_important,
        why_surprising=why_surprising,
        relevant_page_ids=relevant_page_ids,
        epistemic_signals=epistemic_signals,
    )


def test_memo_candidate_accepts_valid_scores():
    c = _candidate(importance=5, surprise=1)
    assert c.importance == 5
    assert c.surprise == 1


@pytest.mark.parametrize("bad", [0, 6, -1, 100])
def test_memo_candidate_rejects_out_of_range_importance(bad):
    with pytest.raises(ValidationError):
        _candidate(importance=bad)


@pytest.mark.parametrize("bad", [0, 6, -1, 100])
def test_memo_candidate_rejects_out_of_range_surprise(bad):
    with pytest.raises(ValidationError):
        _candidate(surprise=bad)


def test_memo_scan_defaults_are_empty():
    scan = MemoScan()
    assert scan.scan_notes == ""
    assert list(scan.candidates) == []
    assert list(scan.excluded) == []


def test_render_scan_summary_handles_empty_candidates():
    out = render_scan_summary(MemoScan(scan_notes="picture is thin"))
    assert "picture is thin" in out
    assert "Memo candidates (0)" in out


def test_render_scan_summary_includes_each_candidates_fields():
    scan = MemoScan(
        scan_notes="whole picture note",
        candidates=[
            _candidate(
                title="Finding A",
                headline_claim="A is true.",
                content_guess="Because of X and Y.",
                importance=4,
                surprise=2,
                relevant_page_ids=["p1234567", "p7654321"],
                epistemic_signals="rests on a Fermi at p1234567",
            ),
        ],
    )
    out = render_scan_summary(scan)
    assert "whole picture note" in out
    assert "Finding A" in out
    assert "A is true." in out
    assert "Because of X and Y." in out
    assert "p1234567" in out
    assert "p7654321" in out
    assert "Fermi" in out
    assert "importance 4" in out
    assert "surprise 2" in out


def test_render_scan_summary_ranks_by_importance_plus_surprise():
    scan = MemoScan(
        candidates=[
            _candidate(title="Low", importance=1, surprise=1),
            _candidate(title="High", importance=5, surprise=5),
            _candidate(title="Mid", importance=3, surprise=3),
        ]
    )
    out = render_scan_summary(scan)
    high_pos = out.index("High")
    mid_pos = out.index("Mid")
    low_pos = out.index("Low")
    assert high_pos < mid_pos < low_pos


def test_render_scan_summary_lists_excluded_findings():
    scan = MemoScan(
        candidates=[],
        excluded=[
            ExcludedFinding(description="Marginal claim", reason="not load-bearing"),
        ],
    )
    out = render_scan_summary(scan)
    assert "Excluded (1)" in out
    assert "Marginal claim" in out
    assert "not load-bearing" in out


def test_render_scan_summary_handles_no_relevant_pages():
    scan = MemoScan(
        candidates=[_candidate(title="Lonely", relevant_page_ids=[])],
    )
    out = render_scan_summary(scan)
    assert "Lonely" in out
    assert "(none)" in out


def test_save_memo_scan_writes_json_round_trippable(monkeypatch, tmp_path):
    monkeypatch.setattr(memos, "MEMO_SCANS_DIR", tmp_path / "memo-scans")
    scan = MemoScan(
        scan_notes="picture",
        candidates=[_candidate(title="X")],
        excluded=[ExcludedFinding(description="y", reason="z")],
    )
    path = save_memo_scan(scan, "Is the sky blue?")
    assert path.exists()
    assert path.suffix == ".json"
    assert "is-the-sky-blue" in path.name
    payload = json.loads(path.read_text(encoding="utf-8"))
    reloaded = MemoScan.model_validate(payload)
    assert reloaded.scan_notes == "picture"
    assert reloaded.candidates[0].title == "X"
    assert reloaded.excluded[0].description == "y"


def test_save_memo_scan_sanitises_headline_for_slug(monkeypatch, tmp_path):
    monkeypatch.setattr(memos, "MEMO_SCANS_DIR", tmp_path / "memo-scans")
    path = save_memo_scan(MemoScan(), "Weird/characters*and?punctuation: here!")
    for bad in "/*?:!":
        assert bad not in path.name


async def test_scan_for_memos_rejects_missing_question(tmp_db):
    with pytest.raises(ValueError, match="not found"):
        await scan_for_memos("00000000-0000-0000-0000-000000000000", tmp_db)


@pytest.mark.llm
async def test_scan_for_memos_end_to_end(tmp_db, question_page):
    """Build a tiny investigation with a couple of considerations and a
    judgement; assert the scanner returns a valid MemoScan. We do not
    assert on specific content — Haiku is unreliable for that — only on
    structural outcomes that any reasonable LLM run should satisfy."""
    consideration = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Frontier models already automate many routine cognitive tasks "
        "in software engineering, suggesting rapid progress.",
        headline="Frontier models already automate routine cognitive tasks in SWE",
        credence=7,
        robustness=3,
    )
    counterweight = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Adoption in non-tech industries lags by years; survey data "
        "suggests less than 10% real use in legal and accounting workflows.",
        headline="Adoption outside tech is much slower than benchmarks suggest",
        credence=6,
        robustness=2,
    )
    judgement = Page(
        page_type=PageType.JUDGEMENT,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Routine cognitive labour will be substantially automated within "
        "5-10 years for tech-adjacent roles, but adoption-limited rather than "
        "capability-limited elsewhere.",
        headline="5-10 year horizon, gated by adoption rather than capability",
        robustness=3,
        extra={
            "key_dependencies": "Hinges on continued frontier-model progress and on adoption rates outside tech matching historical software diffusion.",
            "sensitivity_analysis": "If adoption matches mobile-app S-curve, halve the timeline; if it follows ERP, double it.",
        },
    )
    await tmp_db.save_page(consideration)
    await tmp_db.save_page(counterweight)
    await tmp_db.save_page(judgement)
    await tmp_db.save_link(
        PageLink(
            from_page_id=consideration.id,
            to_page_id=question_page.id,
            link_type=LinkType.CONSIDERATION,
            strength=4,
            direction=ConsiderationDirection.SUPPORTS,
        )
    )
    await tmp_db.save_link(
        PageLink(
            from_page_id=counterweight.id,
            to_page_id=question_page.id,
            link_type=LinkType.CONSIDERATION,
            strength=3,
            direction=ConsiderationDirection.OPPOSES,
        )
    )
    await tmp_db.save_link(
        PageLink(
            from_page_id=judgement.id,
            to_page_id=question_page.id,
            link_type=LinkType.ANSWERS,
        )
    )

    scan = await scan_for_memos(question_page.id, tmp_db)
    assert isinstance(scan, MemoScan)
    assert isinstance(scan.scan_notes, str)
    for c in scan.candidates:
        assert 1 <= c.importance <= 5
        assert 1 <= c.surprise <= 5
        assert c.title.strip()
        assert c.headline_claim.strip()


REPO_ROOT = Path(__file__).resolve().parent.parent


def test_cli_scan_memos_flag_in_help():
    result = subprocess.run(
        ["uv", "run", "python", "main.py", "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0
    assert "--scan-memos" in result.stdout


def test_cli_scan_memos_dispatches_and_handles_missing_id():
    """End-to-end: argparse accepts the flag and cmd_scan_memos runs
    (reports 'not found' for an unknown question id)."""
    scratch_workspace = f"scan-memos-cli-{uuid.uuid4().hex[:8]}"
    result = subprocess.run(
        [
            "uv",
            "run",
            "python",
            "main.py",
            "--scan-memos",
            "nonexistent-id",
            "--workspace",
            scratch_workspace,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    combined = result.stdout + result.stderr
    assert "unrecognized" not in combined.lower()
    assert "not found" in combined.lower()
