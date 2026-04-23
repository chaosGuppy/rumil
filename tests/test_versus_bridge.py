"""Pure-logic tests for rumil.versus_bridge.

No LLM and no DB — everything here is fast and deterministic. The
LLM-dependent code paths (judge_pair_ws_aware / judge_pair_orch) are
exercised via the CLI manually; unit tests of those would either mock
the agent (coupling to internal structure) or spend real tokens per
run (slow, expensive, and mostly validating the SDK layer, not our
code).

Focus here is the surfaces that carry real correctness risk:

- Preference-label parsing (7-point scale -> A/B/tie).
- Prompt hashing (dedup-key discipline).
- Prompt composition (shell + task body).
- Pair content formatting -- especially the regression that source_ids
  (which can literally be ``"human"``) must NOT leak into the prompt.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from rumil.versus_bridge import (
    BLIND_JUDGE_VERSION,
    PREFERENCE_LABELS,
    JudgeResult,
    PairContext,
    _format_pair_content,
    _versus_extra,
    build_system_prompt,
    compute_prompt_hash,
    extract_preference,
    label_to_verdict,
)

_VERSUS_SRC = Path(__file__).resolve().parents[1] / "versus" / "src"
if str(_VERSUS_SRC) not in sys.path:
    sys.path.insert(0, str(_VERSUS_SRC))

from versus.rumil_judge import _build_rumil_text_user_message, _PendingPair  # noqa: E402


def _make_pair(**overrides) -> PairContext:
    defaults = dict(
        essay_id="essay-xyz",
        prefix_hash="prefix-abc",
        prefix_text="Essay opens here.",
        continuation_a_id="human",
        continuation_a_text="Continuation A body: a careful human argument.",
        continuation_b_id="openai/gpt-5.4",
        continuation_b_text="Continuation B body: a model's extension.",
        source_a_id="human",
        source_b_id="openai/gpt-5.4",
        task_name="general_quality",
    )
    defaults.update(overrides)
    return PairContext(**defaults)


# Preference-label parsing --------------------------------------------------


@pytest.mark.parametrize("label", list(PREFERENCE_LABELS))
def test_extract_preference_finds_each_label(label):
    text = f"Some reasoning.\n\n{label}\n"
    assert extract_preference(text) == label


def test_extract_preference_returns_none_when_missing():
    assert extract_preference("An agent that never emitted the scale.") is None


def test_extract_preference_is_case_insensitive():
    # Models sometimes sentence-case or quote the label. The parser
    # should still find it -- matching how ab_eval's extractor works.
    text = "My final rating: 'b somewhat preferred' should be parsed."
    assert extract_preference(text) == "B somewhat preferred"


@pytest.mark.parametrize(
    "label,expected",
    [
        ("A strongly preferred", "A"),
        ("A somewhat preferred", "A"),
        ("A slightly preferred", "A"),
        ("Approximately indifferent between A and B", "tie"),
        ("B slightly preferred", "B"),
        ("B somewhat preferred", "B"),
        ("B strongly preferred", "B"),
    ],
)
def test_label_to_verdict_maps_each_label(label, expected):
    assert label_to_verdict(label) == expected


def test_label_to_verdict_handles_none():
    assert label_to_verdict(None) is None


def test_label_to_verdict_unknown_label_is_none():
    assert label_to_verdict("Sort of a tie I think") is None


# Prompt hashing ------------------------------------------------------------


def test_compute_prompt_hash_is_deterministic():
    body = "Some task body."
    assert compute_prompt_hash(body) == compute_prompt_hash(body)


def test_compute_prompt_hash_changes_with_task_body():
    a = compute_prompt_hash("Task body A")
    b = compute_prompt_hash("Task body B, different text")
    assert a != b


def test_compute_prompt_hash_is_short_hex():
    h = compute_prompt_hash("anything")
    assert len(h) == 8
    assert all(c in "0123456789abcdef" for c in h)


# Prompt composition --------------------------------------------------------


def test_build_system_prompt_slots_task_body_into_shell():
    body = "UNIQUE_TASK_BODY_MARKER_42"
    composed = build_system_prompt(body)
    # The body is inserted into the shell; the shell's static content
    # should also be present (the 7-point scale lives there).
    assert body in composed
    assert "A strongly preferred" in composed
    assert "B strongly preferred" in composed


def test_build_system_prompt_does_not_leave_unfilled_placeholder():
    composed = build_system_prompt("whatever")
    assert "{task_body}" not in composed


# Pair content formatting (regression: no source_id leak) -------------------


def test_format_pair_content_does_not_leak_source_ids_on_continuation_headers():
    # Regression guard: the earlier bug emitted
    # "## Continuation A (source_id: `human`)" which let the judge
    # trivially identify the human continuation. The fix is to drop
    # any source_id annotation from the prompt content. This test
    # pins the specific failure pattern: the continuation header
    # lines must be bare.
    pair = _make_pair(
        continuation_a_id="human",
        source_a_id="human",
        continuation_b_id="openai/gpt-5.4",
        source_b_id="openai/gpt-5.4",
    )
    content = _format_pair_content(pair)

    # Extract the continuation header lines and verify they're bare
    # (no trailing source disclosure on either line).
    header_lines = [line for line in content.splitlines() if line.startswith("## Continuation ")]
    assert header_lines == ["## Continuation A", "## Continuation B"]

    # The literal "source_id" label -- the leak pattern we had -- must
    # not appear anywhere in the formatted content the agent sees.
    assert "source_id" not in content
    # And the raw provider-prefixed source id must not appear either.
    assert "openai/gpt-5.4" not in content


def test_format_pair_content_includes_both_continuations_in_display_order():
    pair = _make_pair(
        continuation_a_text="TEXT_FROM_A_SIDE",
        continuation_b_text="TEXT_FROM_B_SIDE",
    )
    content = _format_pair_content(pair)
    # Both continuation bodies appear, and A precedes B in the content
    # so the agent sees them in display order.
    a_idx = content.index("TEXT_FROM_A_SIDE")
    b_idx = content.index("TEXT_FROM_B_SIDE")
    assert a_idx < b_idx


def test_format_pair_content_includes_essay_prefix_and_dimension():
    pair = _make_pair(prefix_text="PREFIX_SIGIL", task_name="grounding")
    content = _format_pair_content(pair)
    assert "PREFIX_SIGIL" in content
    assert "grounding" in content


# Question-page headline regression ----------------------------------------
#
# Earlier bug: ensure_versus_question composed the Question headline as
# ``Versus: <task> -- <source_a_id> vs <source_b_id> (<essay>)`` which
# renders into the question's view and gets loaded by the agent's tools.
# That defeated blind judging -- observed when an orch run's generated
# view started reasoning about "Opus vs human" explicitly. The fix moved
# source ids out of the headline (and out of the content) into
# ``extra`` only. Guard against regression.


def _make_question_page_synchronously(pair: PairContext):
    """Compose a Page the same way ensure_versus_question does, but
    without touching the DB. Lets us unit-test the headline / content
    shape without async + supabase."""
    from unittest.mock import MagicMock

    import rumil.versus_bridge as vb

    # Stand in for the DB's project_id / run_id fields that
    # ensure_versus_question reads off ``db``.
    fake_db = MagicMock()
    fake_db.project_id = "proj-xyz"
    fake_db.run_id = "run-xyz"
    fake_db.save_page = MagicMock()

    import asyncio

    async def _call_save_page(page):  # pragma: no cover
        fake_db._last_page = page

    fake_db.save_page.side_effect = _call_save_page

    asyncio.run(vb.ensure_versus_question(fake_db, pair))
    return fake_db._last_page


def test_ensure_versus_question_headline_does_not_leak_source_ids():
    pair = _make_pair(
        continuation_a_id="human",
        source_a_id="human",
        continuation_b_id="anthropic/claude-opus-4-7",
        source_b_id="anthropic/claude-opus-4-7",
        task_name="general_quality",
    )
    page = _make_question_page_synchronously(pair)

    # Neither raw source id should appear in the headline.
    assert "human" not in page.headline.lower()
    assert "anthropic/claude-opus-4-7" not in page.headline
    # And the agent-visible content must NOT leak either.
    assert "anthropic/claude-opus-4-7" not in page.content


def test_ensure_versus_question_extra_does_not_leak_source_ids():
    # rumil.context.format_page() renders every key in page.extra as
    # "key: value" lines inline with the page body, so anything in
    # extra is agent-visible. Guard against source ids being stored
    # there (they were, in an early version, which leaked to orch's
    # generated views).
    pair = _make_pair(
        continuation_a_id="human",
        source_a_id="human",
        continuation_b_id="anthropic/claude-opus-4-7",
        source_b_id="anthropic/claude-opus-4-7",
        task_name="general_quality",
    )
    page = _make_question_page_synchronously(pair)

    # Explicit keys that would leak source identity must not be
    # present -- even under safe-sounding names like "source_a_id".
    extra = page.extra
    forbidden_keys = {"source_a_id", "source_b_id", "source_a", "source_b"}
    assert not (extra.keys() & forbidden_keys), (
        f"page.extra must not carry source-identifying keys; got {sorted(extra.keys() & forbidden_keys)}"
    )
    # And no value in extra should be a raw source id either.
    for v in extra.values():
        assert "anthropic/claude-opus-4-7" not in str(v)
        # "human" is a common substring so we guard only the exact
        # token as a value, not substring.
        assert v != "human"


# BLIND_JUDGE_VERSION smoke guard ------------------------------------------


def test_blind_judge_version_is_positive_int():
    assert isinstance(BLIND_JUDGE_VERSION, int)
    assert BLIND_JUDGE_VERSION >= 1


# Prompt hash forks on shell edit ------------------------------------------


def test_compute_prompt_hash_changes_when_shell_file_changes(tmp_path, monkeypatch):
    import rumil.versus_bridge as vb

    body = "Stable task body."
    baseline = compute_prompt_hash(body)

    (tmp_path / "versus-judge-shell.md").write_text(
        "Totally different shell wording here.\n\n{task_body}\n"
    )
    monkeypatch.setattr(vb, "_PROMPTS_DIR", tmp_path)
    forked = compute_prompt_hash(body)

    assert baseline != forked


# Shell composition: exact placeholder token ------------------------------


def test_build_system_prompt_inserts_at_known_placeholder():
    body = "SENTINEL_BODY_TOKEN_7q"
    composed = build_system_prompt(body)
    shell_raw = (
        Path(__file__).resolve().parents[1] / "prompts" / "versus-judge-shell.md"
    ).read_text()
    assert "{task_body}" in shell_raw
    expected = shell_raw.replace("{task_body}", body)
    assert composed == expected


# Question page extra metadata: no source id disclosure -------------------


@pytest.mark.parametrize(
    ("a_id", "b_id"),
    [
        ("human", "anthropic/claude-opus-4-7"),
        ("openai/gpt-5.4", "human"),
        ("anthropic/claude-sonnet-4-5", "openai/gpt-5.4"),
    ],
)
def test_versus_extra_does_not_leak_source_ids(a_id, b_id):
    pair = _make_pair(
        continuation_a_id=a_id,
        continuation_b_id=b_id,
        source_a_id=a_id,
        source_b_id=b_id,
    )
    extra = _versus_extra(pair)

    assert set(extra.keys()) == {"source", "essay_id", "prefix_hash", "task_name"}
    for v in extra.values():
        assert v != a_id
        assert v != b_id
        assert "human" not in str(v).split(":")


# rumil-text inline user message: no source id disclosure ------------------


def test_build_rumil_text_user_message_does_not_leak_source_ids():
    pair = _PendingPair(
        essay_id="essay-xyz",
        prefix_hash="prefix-abc",
        prefix_text="PREFIX_SIGIL",
        source_a_id="human",
        source_a_text="CONT_HUMAN_TEXT",
        source_b_id="anthropic/claude-opus-4-7",
        source_b_text="CONT_OPUS_TEXT",
        display_first_id="anthropic/claude-opus-4-7",
        display_first_text="CONT_OPUS_TEXT",
        display_second_id="human",
        display_second_text="CONT_HUMAN_TEXT",
    )
    msg = _build_rumil_text_user_message(pair, "general_quality")

    assert "anthropic/claude-opus-4-7" not in msg
    assert "source_id" not in msg
    assert "source_a" not in msg
    assert "source_b" not in msg

    a_idx = msg.index("CONT_OPUS_TEXT")
    b_idx = msg.index("CONT_HUMAN_TEXT")
    assert a_idx < b_idx
    assert "PREFIX_SIGIL" in msg
    assert "general_quality" in msg


# JudgeResult contract guard -----------------------------------------------


def test_judge_result_requires_all_fields():
    result = JudgeResult(
        verdict="A",
        preference_label="A strongly preferred",
        reasoning_text="because",
        trace_url="http://example/traces/run-1",
        call_id="call-1",
        run_id="run-1",
        question_id="q-1",
        cost_usd=0.0,
    )
    assert result.verdict == "A"
    assert result.preference_label == "A strongly preferred"

    with pytest.raises(TypeError):
        JudgeResult(verdict="A")  # pyright: ignore[reportCallIssue]


# PREFERENCE_LABELS ordering ------------------------------------------------


def test_preference_labels_are_in_scale_order():
    assert list(PREFERENCE_LABELS) == [
        "A strongly preferred",
        "A somewhat preferred",
        "A slightly preferred",
        "Approximately indifferent between A and B",
        "B slightly preferred",
        "B somewhat preferred",
        "B strongly preferred",
    ]
