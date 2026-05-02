"""Tests for ``versus.tasks.complete_essay.CompleteEssayTask``.

Pure-Python tests: no LLM, no DB. Focus on the contract the task
exposes via the :class:`VersusTask` protocol — fingerprint shape,
question creation, render_for_closer behavior, artifact extraction.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_VERSUS_SRC = Path(__file__).resolve().parents[1] / "versus" / "src"
if str(_VERSUS_SRC) not in sys.path:
    sys.path.insert(0, str(_VERSUS_SRC))

from versus.tasks import (  # noqa: E402
    CompleteEssayTask,
    CompletionArtifact,
    EssayPrefixContext,
    VersusTask,
    compute_completion_closer_hash,
    compute_question_surface_hash,
    compute_tool_prompt_hash,
)


def _make_prefix(
    *,
    essay_id: str = "forethought__broad-timelines",
    prefix_hash: str = "abcdef1234567890",
    prefix_text: str = "The opening of the essay establishes a thesis.",
    target_length_chars: int = 4000,
) -> EssayPrefixContext:
    return EssayPrefixContext(
        essay_id=essay_id,
        prefix_hash=prefix_hash,
        prefix_text=prefix_text,
        target_length_chars=target_length_chars,
    )


def test_complete_essay_task_satisfies_protocol():
    task = CompleteEssayTask()
    assert isinstance(task, VersusTask)


def test_complete_essay_task_name_is_stable():
    assert CompleteEssayTask().name == "complete_essay"


def test_fingerprint_kind_is_complete_essay():
    fp = CompleteEssayTask().fingerprint(_make_prefix())
    assert fp["kind"] == "complete_essay"


def test_fingerprint_includes_required_hashes():
    fp = CompleteEssayTask().fingerprint(_make_prefix())
    assert "tool_prompt_hash" in fp
    assert "question_surface_hash" in fp
    assert "closer_hash" in fp


def test_fingerprint_omits_pair_surface_hash():
    """Completion task has no pair — pair_surface_hash should not appear."""
    fp = CompleteEssayTask().fingerprint(_make_prefix())
    assert "pair_surface_hash" not in fp


def test_fingerprint_question_surface_hash_matches_module_helper():
    fp = CompleteEssayTask().fingerprint(_make_prefix())
    assert fp["question_surface_hash"] == compute_question_surface_hash()


def test_fingerprint_tool_prompt_hash_matches_module_helper():
    fp = CompleteEssayTask().fingerprint(_make_prefix())
    assert fp["tool_prompt_hash"] == compute_tool_prompt_hash()


def test_fingerprint_closer_hash_matches_module_helper():
    fp = CompleteEssayTask().fingerprint(_make_prefix())
    assert fp["closer_hash"] == compute_completion_closer_hash()


def test_fingerprint_is_input_independent():
    """Today's fingerprint covers code/template invariants, not per-essay
    inputs (essay_id / prefix_hash are folded into the row's natural key
    elsewhere). Pin this so a refactor that accidentally hashes inputs
    still surfaces here.
    """
    a = CompleteEssayTask().fingerprint(_make_prefix(essay_id="essay-a"))
    b = CompleteEssayTask().fingerprint(_make_prefix(essay_id="essay-b"))
    assert a == b


def test_closer_prompts_returns_system_and_user_strings():
    task = CompleteEssayTask()
    prefix = _make_prefix()
    rendered = "RENDERED_QUESTION_BODY_SENTINEL"
    system, user = task.closer_prompts(rendered, prefix)
    assert isinstance(system, str)
    assert isinstance(user, str)
    assert system
    assert rendered in user
    assert "4000" in user


def test_closer_prompts_user_includes_continuation_directive():
    task = CompleteEssayTask()
    _, user = task.closer_prompts("rendered", _make_prefix())
    assert "<continuation>" in user
    assert "</continuation>" in user


def test_closer_prompts_user_includes_prefix_text():
    """The closer needs the essay opening to write a continuation. After
    Gap 7 the prefix lives on a linked Source page, not the Question
    content, so closer_prompts must inject inputs.prefix_text directly.
    """
    task = CompleteEssayTask()
    prefix = _make_prefix(prefix_text="MY_DISTINCTIVE_PREFIX_SENTINEL")
    _, user = task.closer_prompts("rendered", prefix)
    assert "MY_DISTINCTIVE_PREFIX_SENTINEL" in user


@pytest.mark.parametrize(
    ("text", "expected_clean"),
    (
        (
            "Outline: foo, bar.\n<continuation>The real continuation.</continuation>",
            "The real continuation.",
        ),
        (
            "<CONTINUATION>uppercase tag content</CONTINUATION>",
            "uppercase tag content",
        ),
        (
            "no tags here just text",
            "no tags here just text",
        ),
        (
            "<continuation>first</continuation>\nscratch\n<continuation>final</continuation>",
            "final",
        ),
    ),
)
def test_extract_artifact_strips_continuation_tags(text, expected_clean):
    task = CompleteEssayTask()
    artifact = task.extract_artifact(text)
    assert isinstance(artifact, CompletionArtifact)
    assert artifact.text == expected_clean
    assert artifact.raw_response == text


def _make_db_mock():
    db = MagicMock()
    db.project_id = "proj-1"
    db.run_id = "run-1"
    db.save_page = AsyncMock()
    db.save_link = AsyncMock()
    return db


def _saved_pages_by_type(db) -> dict:
    """Return saved pages grouped by ``page_type`` for inspection."""
    from rumil.models import PageType  # local import to avoid top-level coupling

    out: dict[PageType, list] = {}
    for call in db.save_page.call_args_list:
        page = call.args[0]
        out.setdefault(page.page_type, []).append(page)
    return out


@pytest.mark.asyncio
async def test_create_question_persists_question_page():
    from rumil.models import PageType

    db = _make_db_mock()
    task = CompleteEssayTask()
    prefix = _make_prefix()
    qid = await task.create_question(db, prefix)
    saved = _saved_pages_by_type(db)
    assert PageType.QUESTION in saved
    question = saved[PageType.QUESTION][0]
    assert qid == question.id
    assert "forethought" not in question.headline
    assert prefix.prefix_hash[:8] in question.headline


@pytest.mark.asyncio
async def test_create_question_extra_excludes_essay_id():
    from rumil.models import PageType

    db = _make_db_mock()
    await CompleteEssayTask().create_question(db, _make_prefix())
    saved = _saved_pages_by_type(db)
    question = saved[PageType.QUESTION][0]
    assert "essay_id" not in question.extra
    assert question.extra.get("task") == "complete_essay"
    assert question.extra.get("source") == "versus"


@pytest.mark.asyncio
async def test_create_question_creates_source_page_with_full_prefix():
    """After Gap 7 the essay opening lives on a linked Source page so
    orch sub-calls find it via embedding-based context retrieval, not
    via load_page recovery on the question.
    """
    from rumil.models import PageType

    db = _make_db_mock()
    prefix = _make_prefix(prefix_text="THE FULL ESSAY OPENING SENTINEL " * 20)
    await CompleteEssayTask().create_question(db, prefix)
    saved = _saved_pages_by_type(db)
    assert PageType.SOURCE in saved
    source = saved[PageType.SOURCE][0]
    assert source.content == prefix.prefix_text


@pytest.mark.asyncio
async def test_create_question_question_content_omits_prefix_text():
    """Regression for Gap 7. The Question's content is framing only —
    the essay opening lives on the linked Source page.
    """
    from rumil.models import PageType

    db = _make_db_mock()
    prefix = _make_prefix(prefix_text="THE_DISTINCTIVE_ESSAY_OPENING_SENTINEL")
    await CompleteEssayTask().create_question(db, prefix)
    saved = _saved_pages_by_type(db)
    question = saved[PageType.QUESTION][0]
    assert prefix.prefix_text not in question.content


@pytest.mark.asyncio
async def test_create_question_links_source_to_question():
    from rumil.models import LinkType, PageType

    db = _make_db_mock()
    qid = await CompleteEssayTask().create_question(db, _make_prefix())
    saved = _saved_pages_by_type(db)
    source = saved[PageType.SOURCE][0]
    db.save_link.assert_called_once()
    link = db.save_link.call_args.args[0]
    assert link.from_page_id == source.id
    assert link.to_page_id == qid
    assert link.link_type == LinkType.RELATED


@pytest.mark.asyncio
async def test_create_question_source_headline_is_source_blind():
    """Source page's headline must not leak the essay_id namespace —
    same blindness rule as the Question headline.
    """
    from rumil.models import PageType

    db = _make_db_mock()
    prefix = _make_prefix(essay_id="forethought__some-leaky-slug")
    await CompleteEssayTask().create_question(db, prefix)
    saved = _saved_pages_by_type(db)
    source = saved[PageType.SOURCE][0]
    assert "forethought" not in source.headline
    assert "some-leaky-slug" not in source.headline
    assert prefix.prefix_hash[:8] in source.headline


@pytest.mark.asyncio
async def test_create_question_source_extra_excludes_essay_id():
    from rumil.models import PageType

    db = _make_db_mock()
    await CompleteEssayTask().create_question(db, _make_prefix())
    saved = _saved_pages_by_type(db)
    source = saved[PageType.SOURCE][0]
    assert "essay_id" not in source.extra


@pytest.mark.asyncio
async def test_render_for_closer_returns_format_page_body_when_no_view(mocker):
    fake_question = MagicMock()
    db = MagicMock()
    db.get_page = AsyncMock(return_value=fake_question)
    db.get_view_for_question = AsyncMock(return_value=None)
    fmt = mocker.patch(
        "versus.tasks.complete_essay.format_page",
        new=AsyncMock(return_value="FORMATTED_QUESTION_BODY"),
    )
    out = await CompleteEssayTask().render_for_closer(db, "q-1")
    assert out == "FORMATTED_QUESTION_BODY"
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
        "versus.tasks.complete_essay.format_page",
        new=AsyncMock(return_value="QUESTION_BODY"),
    )
    mocker.patch(
        "versus.tasks.complete_essay.render_view",
        new=AsyncMock(return_value="VIEW_BODY"),
    )
    out = await CompleteEssayTask().render_for_closer(db, "q-1")
    assert "QUESTION_BODY" in out
    assert "VIEW_BODY" in out


@pytest.mark.asyncio
async def test_render_for_closer_raises_when_question_missing(mocker):
    db = MagicMock()
    db.get_page = AsyncMock(return_value=None)
    with pytest.raises(RuntimeError, match="missing"):
        await CompleteEssayTask().render_for_closer(db, "q-missing")


def test_question_surface_hash_is_stable():
    """Calling the helper twice without code edits should yield the same
    8-char hex digest. Sanity check for the sentinel-based hashing
    approach so a future refactor that introduces nondeterminism (e.g.
    set ordering) surfaces here.
    """
    a = compute_question_surface_hash()
    b = compute_question_surface_hash()
    assert a == b
    assert len(a) == 8


def test_closer_hash_is_stable():
    a = compute_completion_closer_hash()
    b = compute_completion_closer_hash()
    assert a == b
    assert len(a) == 8
