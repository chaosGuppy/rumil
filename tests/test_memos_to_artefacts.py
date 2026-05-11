"""Tests for the memo drafter: memo_mode ContextVar, pure helpers in
memos_to_artefacts, and the MemoOrchestrator's run/resume wrapping.

End-to-end behaviour (the actual generative pipeline) is exercised manually
and reported on by the user; here we stick to pure helpers + structural
guarantees that protect against regressions in shared classes.
"""

import asyncio
import json
from collections.abc import Sequence

import pytest

import rumil.memos_to_artefacts as mta
from rumil.memo_mode import is_memo_mode, memo_mode, memo_source_pages
from rumil.memos import ExcludedFinding, MemoCandidate, MemoScan
from rumil.memos_to_artefacts import (
    MemoOrchestrator,
    _build_task_content,
    _render_scanner_brief,
    _slugify,
    load_scan_from_path,
    save_memo_summary,
    save_memo_to_disk,
)
from rumil.models import (
    Page,
    PageLayer,
    PageType,
    Workspace,
)
from rumil.orchestrators.generative import GenerativeOrchestrator, GenerativeResult


def _candidate(
    *,
    title: str = "Default title",
    headline_claim: str = "A headline.",
    content_guess: str = "A guess.",
    importance: int = 4,
    surprise: int = 3,
    why_important: str = "It is important.",
    why_surprising: str = "It is surprising.",
    relevant_page_ids: Sequence[str] = ("aaaaaaaa", "bbbbbbbb"),
    epistemic_signals: str = "rests on a single source",
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


def _question(headline: str = "Test question", content: str = "Test body") -> Page:
    return Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=content,
        headline=headline,
    )


def test_memo_mode_default_inactive():
    assert is_memo_mode() is False
    assert memo_source_pages() == ""


def test_memo_mode_active_inside_context():
    with memo_mode(source_pages_text="some pages"):
        assert is_memo_mode() is True
        assert memo_source_pages() == "some pages"


def test_memo_mode_resets_after_context():
    with memo_mode(source_pages_text="x"):
        pass
    assert is_memo_mode() is False
    assert memo_source_pages() == ""


def test_memo_mode_resets_even_after_exception():
    with pytest.raises(RuntimeError), memo_mode(source_pages_text="x"):
        raise RuntimeError("boom")
    assert is_memo_mode() is False


def test_memo_mode_with_default_empty_pages_still_marks_mode():
    """Memo mode flag is set even when no source pages are supplied."""
    with memo_mode():
        assert is_memo_mode() is True
        assert memo_source_pages() == ""


async def test_memo_mode_isolates_concurrent_tasks():
    """ContextVars are async-task-local — gather runs see independent state."""
    captured: list[tuple[str, bool, str]] = []

    async def inside_memo():
        with memo_mode(source_pages_text="A"):
            await asyncio.sleep(0.01)
            captured.append(("inside", is_memo_mode(), memo_source_pages()))

    async def outside_memo():
        await asyncio.sleep(0.005)
        captured.append(("outside", is_memo_mode(), memo_source_pages()))

    await asyncio.gather(inside_memo(), outside_memo())
    assert ("inside", True, "A") in captured
    assert ("outside", False, "") in captured


def test_build_task_content_includes_original_question():
    q = _question(headline="Why X?", content="Long body about X.")
    out = _build_task_content(q, _candidate(title="A finding"))
    assert "Why X?" in out
    assert "Long body about X." in out


def test_build_task_content_includes_candidate_title_only():
    """Task description carries the candidate title — not other candidate fields."""
    candidate = _candidate(
        title="The interesting finding",
        headline_claim="HEADLINE_CLAIM_MARKER",
        content_guess="CONTENT_GUESS_MARKER",
        why_important="WHY_IMPORTANT_MARKER",
        epistemic_signals="EPISTEMIC_SIGNALS_MARKER",
    )
    out = _build_task_content(_question(), candidate)
    assert "The interesting finding" in out
    for marker in (
        "HEADLINE_CLAIM_MARKER",
        "CONTENT_GUESS_MARKER",
        "WHY_IMPORTANT_MARKER",
        "EPISTEMIC_SIGNALS_MARKER",
    ):
        assert marker not in out, (
            f"task description leaked candidate field marker {marker!r} — "
            "candidate fields should reach the spec writer via the brief, "
            "not the task description"
        )


def test_build_task_content_includes_memo_guidance():
    out = _build_task_content(_question(), _candidate())
    # Length, voice, and confidence-handling rules must be present so the
    # artefact writer and critics see them, not just the spec writer.
    assert "500 words" in out
    assert "Toby Ord" in out
    assert "hypothesis" in out.lower()


def test_render_scanner_brief_includes_all_candidate_fields():
    candidate = _candidate(
        headline_claim="HC",
        content_guess="CG",
        importance=5,
        surprise=2,
        why_important="WI",
        why_surprising="WS",
        epistemic_signals="ES",
    )
    out = _render_scanner_brief(candidate)
    for fragment in ("HC", "CG", "5/5", "2/5", "WI", "WS", "ES"):
        assert fragment in out


def test_slugify_strips_punctuation():
    assert _slugify("What's the deal?!") == "whats-the-deal"


def test_slugify_lowercases_and_hyphenates_spaces():
    assert _slugify("Some Mixed Case Title") == "some-mixed-case-title"


def test_slugify_truncates_to_max_len():
    long = "a" * 200
    out = _slugify(long, max_len=20)
    assert len(out) <= 20


def test_slugify_falls_back_to_default_when_empty():
    assert _slugify("???!!!") == "memo"
    assert _slugify("") == "memo"


def _artefact(headline: str = "Memo headline", content: str = "Memo body") -> Page:
    return Page(
        page_type=PageType.ARTEFACT,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        headline=headline,
        content=content,
    )


def test_save_memo_to_disk_writes_file_with_metadata(monkeypatch, tmp_path):
    monkeypatch.setattr(mta, "MEMOS_DIR", tmp_path / "memos")
    candidate = _candidate(title="Some finding")
    artefact = _artefact(headline="The memo", content="# Inner heading\n\nbody")
    path = save_memo_to_disk(
        artefact,
        candidate,
        root_question_id="abcdef0123456789",
        root_question_headline="Investigation Q",
        candidate_index=3,
    )
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert path.name.startswith("03-")
    assert "some-finding" in path.name
    assert "Drafted from candidate" in text
    assert "Investigation Q" in text


def test_save_memo_to_disk_does_not_double_heading_when_content_starts_with_h1(
    monkeypatch, tmp_path
):
    """If the model already produced an H1 in content, don't prepend the headline.

    Regression: an earlier version always wrote `# {artefact.headline}` ahead
    of content, which doubled headings whenever the artefact body led with
    its own `# ...` line (which it usually does).
    """
    monkeypatch.setattr(mta, "MEMOS_DIR", tmp_path / "memos")
    artefact = _artefact(
        headline="Wrapper headline",
        content="# The model's own heading\n\nbody",
    )
    path = save_memo_to_disk(
        artefact,
        _candidate(),
        root_question_id="rq" + "0" * 14,
        root_question_headline="ignored",
        candidate_index=1,
    )
    text = path.read_text(encoding="utf-8")
    assert text.count("# The model's own heading") == 1
    assert "# Wrapper headline" not in text  # not duplicated


def test_save_memo_to_disk_adds_heading_when_content_has_no_h1(monkeypatch, tmp_path):
    monkeypatch.setattr(mta, "MEMOS_DIR", tmp_path / "memos")
    artefact = _artefact(
        headline="The memo title",
        content="just a body, no heading",
    )
    path = save_memo_to_disk(
        artefact,
        _candidate(),
        root_question_id="rq" + "0" * 14,
        root_question_headline="ignored",
        candidate_index=1,
    )
    text = path.read_text(encoding="utf-8")
    assert "# The memo title" in text
    assert "just a body, no heading" in text


def test_save_memo_summary_writes_file(monkeypatch, tmp_path):
    monkeypatch.setattr(mta, "MEMOS_DIR", tmp_path / "memos")
    path = save_memo_summary(
        "Summary text body.",
        root_question_id="abcdef01" + "0" * 28,
        root_question_headline="An investigation",
    )
    assert path.exists()
    assert path.name == "00-summary.md"
    text = path.read_text(encoding="utf-8")
    assert "Summary text body." in text
    assert "An investigation" in text  # in metadata comment


def test_load_scan_from_path_roundtrips(tmp_path):
    scan = MemoScan(
        scan_notes="notes",
        candidates=[_candidate(title="A"), _candidate(title="B")],
        excluded=[ExcludedFinding(description="x", reason="y")],
        root_question_id="rq" + "0" * 14,
        root_question_headline="hello",
    )
    path = tmp_path / "scan.json"
    path.write_text(json.dumps(scan.model_dump(mode="json"), indent=2), encoding="utf-8")
    reloaded = load_scan_from_path(path)
    assert reloaded.scan_notes == "notes"
    assert reloaded.root_question_id == scan.root_question_id
    assert [c.title for c in reloaded.candidates] == ["A", "B"]
    assert reloaded.excluded[0].description == "x"


async def test_memo_orchestrator_run_activates_memo_mode_then_resets(mocker):
    """MemoOrchestrator.run() must wrap the parent's run() in memo_mode so
    closing reviews and CritiqueContext see the flag — and must reset it
    cleanly afterwards so other code paths aren't polluted.
    """
    captured: dict[str, object] = {}

    async def fake_run(self, request: str, *, headline: str | None = None):
        captured["memo_mode"] = is_memo_mode()
        captured["source_pages"] = memo_source_pages()
        return GenerativeResult(task_id="t", artefact_id=None, finalized=False)

    mocker.patch.object(GenerativeOrchestrator, "run", fake_run)

    orch = MemoOrchestrator(
        mocker.MagicMock(),
        brief="brief text",
        source_pages_text="SOURCE_PAGES_BLOCK",
    )

    assert is_memo_mode() is False
    result = await orch.run("hello", headline="h")
    assert isinstance(result, GenerativeResult)
    assert captured["memo_mode"] is True
    assert captured["source_pages"] == "SOURCE_PAGES_BLOCK"
    # Reset cleanly afterwards so siblings in concurrent gather don't see leakage.
    assert is_memo_mode() is False


async def test_memo_orchestrator_resume_activates_memo_mode_then_resets(mocker):
    captured: dict[str, object] = {}

    async def fake_resume(self, task_id: str):
        captured["memo_mode"] = is_memo_mode()
        captured["source_pages"] = memo_source_pages()
        return GenerativeResult(task_id=task_id, artefact_id=None, finalized=False)

    mocker.patch.object(GenerativeOrchestrator, "resume", fake_resume)

    orch = MemoOrchestrator(
        mocker.MagicMock(),
        brief="b",
        source_pages_text="SP",
    )

    assert is_memo_mode() is False
    await orch.resume("task-xyz")
    assert captured["memo_mode"] is True
    assert captured["source_pages"] == "SP"
    assert is_memo_mode() is False
