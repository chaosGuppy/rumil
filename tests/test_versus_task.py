"""Tests for ``versus.tasks.judge_pair.JudgePairTask``.

Pure-Python tests: no LLM, no DB. Focus on the contract the task
exposes via the :class:`VersusTask` protocol — fingerprint shape,
prompt rendering, artifact extraction.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_VERSUS_SRC = Path(__file__).resolve().parents[1] / "versus" / "src"
if str(_VERSUS_SRC) not in sys.path:
    sys.path.insert(0, str(_VERSUS_SRC))

from versus.tasks import JudgePairTask, PairContext, VersusTask  # noqa: E402
from versus.tasks.judge_pair import (  # noqa: E402
    JudgeArtifact,
    compute_closer_hash,
    compute_pair_surface_hash,
    compute_tool_prompt_hash,
)


def _make_pair(**overrides) -> PairContext:
    defaults = dict(
        essay_id="essay-xyz",
        prefix_hash="prefix-abc",
        prefix_text="Essay opens here.",
        continuation_a_id="human",
        continuation_a_text="Continuation A body.",
        continuation_b_id="openai/gpt-5.4",
        continuation_b_text="Continuation B body.",
        source_a_id="human",
        source_b_id="openai/gpt-5.4",
        task_name="general_quality",
    )
    defaults.update(overrides)
    return PairContext(**defaults)


def test_judge_pair_task_satisfies_protocol():
    task = JudgePairTask(dimension="general_quality", dimension_body="task body")
    assert isinstance(task, VersusTask)


def test_judge_pair_task_name_is_stable():
    task = JudgePairTask(dimension="grounding", dimension_body="x")
    assert task.name == "judge_pair"


def test_fingerprint_includes_kind_and_dimension():
    task = JudgePairTask(dimension="general_quality", dimension_body="body")
    fp = task.fingerprint(_make_pair())
    assert fp["kind"] == "judge_pair"
    assert fp["dimension"] == "general_quality"


def test_fingerprint_includes_all_required_hashes():
    task = JudgePairTask(dimension="general_quality", dimension_body="body")
    fp = task.fingerprint(_make_pair())
    assert "prompt_hash" in fp
    assert "tool_prompt_hash" in fp
    assert "pair_surface_hash" in fp
    assert "closer_hash" in fp


def test_fingerprint_changes_with_dimension():
    a = JudgePairTask(dimension="general_quality", dimension_body="body").fingerprint(_make_pair())
    b = JudgePairTask(dimension="grounding", dimension_body="body").fingerprint(_make_pair())
    assert a != b


def test_fingerprint_changes_with_dimension_body():
    a = JudgePairTask(dimension="general_quality", dimension_body="body A").fingerprint(
        _make_pair()
    )
    b = JudgePairTask(dimension="general_quality", dimension_body="body B").fingerprint(
        _make_pair()
    )
    assert a["prompt_hash"] != b["prompt_hash"]


def test_fingerprint_pair_surface_hash_matches_module_helper():
    task = JudgePairTask(dimension="general_quality", dimension_body="body")
    fp = task.fingerprint(_make_pair())
    assert fp["pair_surface_hash"] == compute_pair_surface_hash()


def test_fingerprint_tool_prompt_hash_matches_module_helper():
    task = JudgePairTask(dimension="general_quality", dimension_body="body")
    fp = task.fingerprint(_make_pair())
    assert fp["tool_prompt_hash"] == compute_tool_prompt_hash()


def test_fingerprint_closer_hash_matches_module_helper():
    task = JudgePairTask(dimension="general_quality", dimension_body="body")
    fp = task.fingerprint(_make_pair())
    assert fp["closer_hash"] == compute_closer_hash()


def test_closer_prompts_returns_system_and_user_strings():
    task = JudgePairTask(dimension="general_quality", dimension_body="task body sentinel")
    rendered = "RENDERED_QUESTION_BODY_SENTINEL"
    system, user = task.closer_prompts(rendered, _make_pair())
    assert isinstance(system, str)
    assert isinstance(user, str)
    # Task body lives in the system prompt.
    assert "task body sentinel" in system
    # Rendered context lives in the user prompt.
    assert rendered in user


def test_closer_prompts_user_includes_label_directive():
    task = JudgePairTask(dimension="general_quality", dimension_body="x")
    _, user = task.closer_prompts("rendered", _make_pair())
    assert "7-point preference label" in user


@pytest.mark.parametrize(
    ("text", "expected_label", "expected_verdict"),
    (
        ("My verdict: A strongly preferred", "A strongly preferred", "A"),
        ("Final: B somewhat preferred", "B somewhat preferred", "B"),
        (
            "Approximately indifferent between A and B",
            "Approximately indifferent between A and B",
            "tie",
        ),
        ("No label here at all", None, None),
    ),
)
def test_extract_artifact_parses_label_and_verdict(text, expected_label, expected_verdict):
    task = JudgePairTask(dimension="general_quality", dimension_body="x")
    artifact = task.extract_artifact(text)
    assert isinstance(artifact, JudgeArtifact)
    assert artifact.preference_label == expected_label
    assert artifact.verdict == expected_verdict
    assert artifact.reasoning_text == text


@pytest.mark.asyncio
async def test_create_question_persists_page_via_db_save_page(mocker):
    """Task should call db.save_page with a Question page; returns its id."""
    db = MagicMock()
    db.project_id = "proj-1"
    db.run_id = "run-1"
    db.save_page = AsyncMock()
    task = JudgePairTask(dimension="general_quality", dimension_body="x")
    pair = _make_pair()
    qid = await task.create_question(db, pair)
    db.save_page.assert_called_once()
    saved_page = db.save_page.call_args.args[0]
    assert qid == saved_page.id
    assert saved_page.headline == f"Versus judgment: general_quality [{pair.prefix_hash[:8]}]"
    # Source ids must NOT leak into headline / content.
    assert "openai/gpt-5.4" not in saved_page.headline
    assert "openai/gpt-5.4" not in saved_page.content


@pytest.mark.asyncio
async def test_render_for_closer_returns_format_page_body_when_no_view(mocker):
    """When no View exists, render_for_closer returns format_page output."""
    fake_question = MagicMock()
    db = MagicMock()
    db.get_page = AsyncMock(return_value=fake_question)
    db.get_view_for_question = AsyncMock(return_value=None)
    fmt = mocker.patch(
        "versus.tasks.judge_pair.format_page",
        new=AsyncMock(return_value="FORMATTED_PAGE_BODY"),
    )
    task = JudgePairTask(dimension="general_quality", dimension_body="x")
    out = await task.render_for_closer(db, "q-1")
    assert out == "FORMATTED_PAGE_BODY"
    fmt.assert_called_once()


@pytest.mark.asyncio
async def test_render_for_closer_appends_view_when_present(mocker):
    fake_question = MagicMock()
    fake_view = MagicMock()
    fake_view.id = "view-1"
    fake_items = [MagicMock(), MagicMock()]
    db = MagicMock()
    db.get_page = AsyncMock(return_value=fake_question)
    db.get_view_for_question = AsyncMock(return_value=fake_view)
    db.get_view_items = AsyncMock(return_value=fake_items)
    mocker.patch(
        "versus.tasks.judge_pair.format_page",
        new=AsyncMock(return_value="QUESTION_BODY"),
    )
    mocker.patch(
        "versus.tasks.judge_pair.render_view",
        new=AsyncMock(return_value="VIEW_BODY"),
    )
    task = JudgePairTask(dimension="general_quality", dimension_body="x")
    out = await task.render_for_closer(db, "q-1")
    assert "QUESTION_BODY" in out
    assert "VIEW_BODY" in out


@pytest.mark.asyncio
async def test_render_for_closer_raises_when_question_missing(mocker):
    db = MagicMock()
    db.get_page = AsyncMock(return_value=None)
    task = JudgePairTask(dimension="general_quality", dimension_body="x")
    with pytest.raises(RuntimeError, match="missing"):
        await task.render_for_closer(db, "q-missing")
