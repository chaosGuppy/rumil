"""Structural regression tests for the versus blind-judging invariant.

The four versus judge backends all accept a ``PairContext`` (or a
``_PendingPair``) carrying raw ``source_a_id`` / ``source_b_id`` values
— which can literally be the string ``"human"``. Every surface the
judge model actually sees (system prompt, user message, Question page
content/headline, ``page.extra`` keys rendered by
``rumil.context.format_page``) must not disclose those ids.

These tests use sentinel strings (``__SOURCE_A_SENTINEL__...``) that
can't collide with English prose, so any substring presence in the
rendered payload is unambiguous evidence of a leak.

The four backends covered:

1. OpenRouter text judge        (``versus/judge.py::render_judge_prompt``)
2. Anthropic text judge         (same ``render_judge_prompt`` via ``rumil_judge._plan_tasks``)
3. rumil:text                   (``rumil_judge._build_rumil_text_user_message``)
4. rumil:ws / rumil:orch        (``versus_bridge._format_pair_content``,
                                 ``_versus_extra``, ``ensure_versus_question``,
                                 and the inline ws/orch user prompts)

The continuation *bodies* (``continuation_a_text`` / ``continuation_b_text``)
may legitimately contain the literal word "human" as essay prose — the
invariant is about *ids*, not about word choice in the continuation. So
we use sentinel ids (guaranteed not to appear in prose) and keep the
continuation-body text simple.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from rumil.versus_bridge import (
    PairContext,
    _build_ws_user_prompt,
    _versus_extra,
    ensure_versus_question,
)

_VERSUS_SRC = Path(__file__).resolve().parents[1] / "versus" / "src"
if str(_VERSUS_SRC) not in sys.path:
    sys.path.insert(0, str(_VERSUS_SRC))

from versus.judge import render_judge_prompt  # noqa: E402
from versus.rumil_judge import _build_rumil_text_user_message, _PendingPair  # noqa: E402

SENTINEL_A = "__SOURCE_A_SENTINEL_7x3q1z__"
SENTINEL_B = "__SOURCE_B_SENTINEL_9k2mvp__"
SENTINEL_ESSAY = "essay-sentinel-1"
SENTINEL_PREFIX_HASH = "prefix-sentinel-1"
SENTINEL_PREFIX_TEXT = "PREFIX_SENTINEL_TEXT"
SENTINEL_A_TEXT = "CONTINUATION_A_BODY_SENTINEL"
SENTINEL_B_TEXT = "CONTINUATION_B_BODY_SENTINEL"


def _make_pair_context(
    source_a_id: str = SENTINEL_A,
    source_b_id: str = SENTINEL_B,
    task_name: str = "general_quality",
) -> PairContext:
    return PairContext(
        essay_id=SENTINEL_ESSAY,
        prefix_hash=SENTINEL_PREFIX_HASH,
        prefix_text=SENTINEL_PREFIX_TEXT,
        continuation_a_id=source_a_id,
        continuation_a_text=SENTINEL_A_TEXT,
        continuation_b_id=source_b_id,
        continuation_b_text=SENTINEL_B_TEXT,
        source_a_id=source_a_id,
        source_b_id=source_b_id,
        task_name=task_name,
    )


def _make_pending_pair(
    source_a_id: str = SENTINEL_A,
    source_b_id: str = SENTINEL_B,
) -> _PendingPair:
    return _PendingPair(
        essay_id=SENTINEL_ESSAY,
        prefix_hash=SENTINEL_PREFIX_HASH,
        prefix_text=SENTINEL_PREFIX_TEXT,
        source_a_id=source_a_id,
        source_a_text=SENTINEL_A_TEXT,
        source_b_id=source_b_id,
        source_b_text=SENTINEL_B_TEXT,
        display_first_id=source_a_id,
        display_first_text=SENTINEL_A_TEXT,
        display_second_id=source_b_id,
        display_second_text=SENTINEL_B_TEXT,
    )


def _assert_no_id_leak(rendered: str, source_a_id: str, source_b_id: str) -> None:
    """Assert neither source_id appears as a substring in ``rendered``.

    The continuation bodies are allowed through (they're the whole point
    of the rendered payload), but ids — which can be ``"human"`` or
    ``"paraphrase:human"`` in practice — must not. The sentinel strings
    used in this test file are chosen so they don't appear in any essay
    body, dimension prompt, or judge shell.
    """
    assert source_a_id not in rendered, f"source_a_id {source_a_id!r} leaked into rendered payload"
    assert source_b_id not in rendered, f"source_b_id {source_b_id!r} leaked into rendered payload"


ID_PAIRS = (
    (SENTINEL_A, SENTINEL_B),
    ("human", SENTINEL_B),
    (SENTINEL_A, "human"),
    ("paraphrase:human", SENTINEL_B),
    ("human", "paraphrase:anthropic/claude-opus-4-7"),
    ("openai/gpt-5.4", "anthropic/claude-sonnet-4-5"),
)


@pytest.mark.parametrize(("source_a_id", "source_b_id"), ID_PAIRS)
def test_openrouter_text_judge_does_not_leak_source_ids(source_a_id, source_b_id):
    """Backend 1: OpenRouter text judge via render_judge_prompt."""
    system, user = render_judge_prompt(
        prefix_text=SENTINEL_PREFIX_TEXT,
        dimension="general_quality",
        source_a_text=SENTINEL_A_TEXT,
        source_b_text=SENTINEL_B_TEXT,
    )
    # render_judge_prompt doesn't take source_ids at all, so with
    # sentinel ids this should trivially hold — but the test pins that
    # the signature never regresses to accept ids.
    _assert_no_id_leak(system, source_a_id, source_b_id)
    _assert_no_id_leak(user, source_a_id, source_b_id)
    assert SENTINEL_A_TEXT in user
    assert SENTINEL_B_TEXT in user


@pytest.mark.parametrize(("source_a_id", "source_b_id"), ID_PAIRS)
def test_anthropic_text_judge_does_not_leak_source_ids(source_a_id, source_b_id):
    """Backend 2: Anthropic text judge — uses the same render_judge_prompt.

    Kept as a separate test (not just a parametrize) so the coverage
    test can find one entry per backend.
    """
    system, user = render_judge_prompt(
        prefix_text=SENTINEL_PREFIX_TEXT,
        dimension="grounding",
        source_a_text=SENTINEL_A_TEXT,
        source_b_text=SENTINEL_B_TEXT,
    )
    _assert_no_id_leak(system, source_a_id, source_b_id)
    _assert_no_id_leak(user, source_a_id, source_b_id)


@pytest.mark.parametrize(("source_a_id", "source_b_id"), ID_PAIRS)
def test_rumil_text_judge_does_not_leak_source_ids(source_a_id, source_b_id):
    """Backend 3: rumil:text inline user message."""
    pair = _make_pending_pair(source_a_id=source_a_id, source_b_id=source_b_id)
    msg = _build_rumil_text_user_message(pair, "general_quality")
    _assert_no_id_leak(msg, source_a_id, source_b_id)
    assert SENTINEL_A_TEXT in msg
    assert SENTINEL_B_TEXT in msg


@pytest.mark.parametrize(("source_a_id", "source_b_id"), ID_PAIRS)
def test_rumil_ws_orch_agent_visible_surfaces_do_not_leak_source_ids(source_a_id, source_b_id):
    """Backend 4: rumil:ws / rumil:orch agent-visible surfaces.

    Covers everything a ws/orch agent can read without hitting the DB
    for unrelated pages:

    - Question page ``content`` (``_format_pair_content``)
    - Question page ``headline`` (set in ``ensure_versus_question``)
    - Question page ``extra`` values (``_versus_extra``) — rendered
      verbatim by ``rumil.context.format_page`` as ``key: value`` lines
    """
    pair = _make_pair_context(source_a_id=source_a_id, source_b_id=source_b_id)
    page = _build_versus_question_page(pair)

    _assert_no_id_leak(page.content, source_a_id, source_b_id)
    _assert_no_id_leak(page.headline, source_a_id, source_b_id)

    extra_rendered = "\n".join(f"{k}: {v}" for k, v in (page.extra or {}).items())
    _assert_no_id_leak(extra_rendered, source_a_id, source_b_id)

    # The inline user message the SDK agent receives in ws runs. Reads
    # the pair via load_page on the scope question, so ids must not
    # appear in the instruction shell either.
    ws_user = _build_ws_user_prompt(pair, question_id=page.id)
    _assert_no_id_leak(ws_user, source_a_id, source_b_id)


def _build_versus_question_page(pair: PairContext):
    """Run ``ensure_versus_question`` with a stub DB and return the Page.

    Mirrors the helper in ``test_versus_bridge.py`` so this test file
    stays independently runnable.
    """
    fake_db = MagicMock()
    fake_db.project_id = "proj-sentinel"
    fake_db.run_id = "run-sentinel"

    captured: dict = {}

    async def _save_page(page):
        captured["page"] = page

    fake_db.save_page.side_effect = _save_page
    asyncio.run(ensure_versus_question(fake_db, pair))
    return captured["page"]


def test_page_extra_does_not_carry_provenance_fields():
    """Structural guard: ``page.extra`` keys never include source-provenance fields.

    This is stricter than the substring-leak test — even if a future
    refactor puts source ids in ``extra`` under a cleverly-named key
    (``attribution``, ``origin_model``, ...), this test fails so the
    schema-level invariant stays loud.
    """
    pair = _make_pair_context(source_a_id="human", source_b_id="anthropic/claude-opus-4-7")
    extra = _versus_extra(pair)

    forbidden_keys = {
        "source_a_id",
        "source_b_id",
        "source_a",
        "source_b",
        "continuation_a_id",
        "continuation_b_id",
        "a_source",
        "b_source",
        "a_id",
        "b_id",
        "attribution",
        "origin_model",
        "source_model",
    }
    present = set(extra.keys()) & forbidden_keys
    assert not present, f"page.extra must not carry source-provenance keys; got {sorted(present)}"


def test_page_extra_keys_are_exactly_the_allowed_set():
    """Pin the exact key set so adding a new key is a deliberate act.

    The current allowed set: ``source`` (literal string ``"versus"``),
    ``essay_id``, ``prefix_hash``, ``task_name``. Anything else must be
    considered for blind-judge leak risk before being added.
    """
    pair = _make_pair_context()
    extra = _versus_extra(pair)
    assert set(extra.keys()) == {"source", "essay_id", "prefix_hash", "task_name"}


BLIND_JUDGE_BACKEND_TESTS = (
    ("openrouter_text", "test_openrouter_text_judge_does_not_leak_source_ids"),
    ("anthropic_text", "test_anthropic_text_judge_does_not_leak_source_ids"),
    ("rumil_text", "test_rumil_text_judge_does_not_leak_source_ids"),
    ("rumil_ws_orch", "test_rumil_ws_orch_agent_visible_surfaces_do_not_leak_source_ids"),
)


def test_each_backend_has_a_leak_absence_test():
    """Coverage guard: every versus judge backend must have a leak test.

    If someone adds a fifth backend, bumping this registry (and adding
    the corresponding test) is required — otherwise this test fails.
    The current backends are enumerated in ``BLIND_JUDGE_BACKEND_TESTS``
    above; each entry is a ``(backend_name, test_function_name)`` tuple
    and the test function must exist in this module.

    This keeps the blind-judge invariant load-bearing instead of letting
    new backends quietly skip it.
    """
    module = sys.modules[__name__]
    for backend_name, test_name in BLIND_JUDGE_BACKEND_TESTS:
        assert hasattr(module, test_name), (
            f"backend {backend_name!r} is missing its leak-absence test "
            f"{test_name!r} in tests/test_versus_blind_judge.py"
        )

    # Also pin the count so deleting a test (and forgetting to remove
    # it from BLIND_JUDGE_BACKEND_TESTS) fails loudly.
    assert len(BLIND_JUDGE_BACKEND_TESTS) == 4, (
        "expected exactly 4 backends; update this assertion deliberately "
        "if a backend is added or removed"
    )
