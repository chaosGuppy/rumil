"""Tests for editor truncation recovery in DraftAndEditWorkflow.

When the editor's response opens ``<continuation>`` but never emits
the closing tag (max_tokens cut it off mid-revision), the workflow
should re-fire the editor with a multi-turn message stack asking it
to continue, concatenating responses until either the closing tag
appears or ``max_attempts`` is exhausted.

Discovered after the round-1 iterate session: character × harsher_critic
lost "strongly preferred for human" because the editor ran out of
tokens during the verbose <preserved> + <cuts> scaffolding and the
recorded continuation ended mid-paragraph at ~2,348 words while the
human reference was 4,470 words. Fresh re-fires of the same exchange
produced ~4,100-word complete continuations.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_VERSUS_SRC = Path(__file__).resolve().parents[1] / "versus" / "src"
if str(_VERSUS_SRC) not in sys.path:
    sys.path.insert(0, str(_VERSUS_SRC))

from rumil.model_config import ModelConfig  # noqa: E402
from rumil.orchestrators.draft_and_edit import (  # noqa: E402
    DraftAndEditWorkflow,
    _is_truncated_continuation,
)
from rumil.settings import override_settings  # noqa: E402

# Truncation detector — pure function, no mocks needed.


def test_is_truncated_open_no_close_returns_true():
    text = "<preserved>note</preserved><continuation>essay body without ending"
    assert _is_truncated_continuation(text) is True


def test_is_truncated_closed_block_returns_false():
    text = "<continuation>complete essay body</continuation>"
    assert _is_truncated_continuation(text) is False


def test_is_truncated_no_tags_returns_false():
    """Unstructured response with no continuation tags isn't 'truncated'
    in the sense that matters here — the extractor falls back to the
    whole text. Re-firing wouldn't help."""
    text = "Just some prose without any continuation markup."
    assert _is_truncated_continuation(text) is False


def test_is_truncated_closed_then_trailing_open_returns_false():
    """A complete block followed by a stray opening tag is fine —
    the closed block already carries a usable revision; the trailing
    open is scratch."""
    text = "<continuation>complete</continuation>\n<continuation>scratch"
    assert _is_truncated_continuation(text) is False


def test_is_truncated_empty_string_returns_false():
    assert _is_truncated_continuation("") is False


# Continuation loop.


@pytest.fixture(autouse=True)
def _model_override():
    """`_resolve_model` requires a per-role override or settings.rumil_model_override."""

    with override_settings(rumil_model_override="claude-sonnet-4-6"):
        yield


def _make_workflow() -> DraftAndEditWorkflow:
    return DraftAndEditWorkflow(budget=4)


def _mock_loop_deps():
    db = MagicMock()
    trace = MagicMock()
    trace.record = AsyncMock()
    return db, trace


@pytest.mark.asyncio
async def test_already_complete_skips_continuation_loop(mocker):
    """If the initial response already has a closing tag, no extra
    text_call invocations should fire."""
    fake_text_call = mocker.patch(
        "rumil.orchestrators.draft_and_edit.text_call",
        new=AsyncMock(return_value="should-not-fire"),
    )
    db, trace = _mock_loop_deps()
    wf = _make_workflow()

    initial = "<continuation>already complete body</continuation>"
    result = await wf._continue_editor_until_complete(
        db=db,
        trace=trace,
        call_id="call-1",
        round_idx=2,
        initial_user_message="prompt body",
        initial_response=initial,
        model="claude-sonnet-4-6",
        editor_kwargs={"cache": True, "max_tokens": 32_000},
    )

    assert result == initial
    fake_text_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_one_continuation_completes_truncated_response(mocker):
    """A single follow-up call that returns a closing tag should
    terminate the loop with a concatenated full response."""
    follow_up = " rest of the body</continuation>"
    fake_text_call = mocker.patch(
        "rumil.orchestrators.draft_and_edit.text_call",
        new=AsyncMock(return_value=follow_up),
    )
    db, trace = _mock_loop_deps()
    wf = _make_workflow()

    initial = "<preserved>p</preserved><continuation>partial body"
    result = await wf._continue_editor_until_complete(
        db=db,
        trace=trace,
        call_id="call-1",
        round_idx=2,
        initial_user_message="prompt body",
        initial_response=initial,
        model="claude-sonnet-4-6",
        editor_kwargs={"cache": True, "max_tokens": 32_000},
    )

    assert result == initial + follow_up
    assert fake_text_call.await_count == 1


@pytest.mark.asyncio
async def test_max_attempts_caps_loop_when_responses_remain_truncated(mocker):
    """If every follow-up itself returns truncated text, the loop must
    stop after ``max_attempts`` so a pathological model can't burn
    indefinitely."""
    truncated_followup = " more body without ending"
    fake_text_call = mocker.patch(
        "rumil.orchestrators.draft_and_edit.text_call",
        new=AsyncMock(return_value=truncated_followup),
    )
    db, trace = _mock_loop_deps()
    wf = _make_workflow()

    initial = "<continuation>partial body"
    result = await wf._continue_editor_until_complete(
        db=db,
        trace=trace,
        call_id="call-1",
        round_idx=2,
        initial_user_message="prompt body",
        initial_response=initial,
        model="claude-sonnet-4-6",
        editor_kwargs={"cache": True, "max_tokens": 32_000},
        max_attempts=2,
    )

    # Concatenated three pieces: initial + 2 follow-ups.
    assert result == initial + truncated_followup + truncated_followup
    assert fake_text_call.await_count == 2
    # Result is still truncated — caller's _extract_continuation falls
    # back to the open-tag path.
    assert _is_truncated_continuation(result) is True


@pytest.mark.asyncio
async def test_followup_message_stack_is_multi_turn(mocker):
    """The follow-up call should pass a message stack with the original
    user message + the partial assistant response + a continuation
    nudge. We don't assert on the exact wording of the nudge — just
    that it exists and the shape is right."""
    fake_text_call = mocker.patch(
        "rumil.orchestrators.draft_and_edit.text_call",
        new=AsyncMock(return_value="rest</continuation>"),
    )
    db, trace = _mock_loop_deps()
    wf = _make_workflow()

    user_prompt = "Edit this draft."
    partial = "<continuation>start"
    await wf._continue_editor_until_complete(
        db=db,
        trace=trace,
        call_id="call-1",
        round_idx=2,
        initial_user_message=user_prompt,
        initial_response=partial,
        model="claude-sonnet-4-6",
        editor_kwargs={"cache": True, "max_tokens": 32_000},
    )

    fake_text_call.assert_awaited_once()
    kwargs = fake_text_call.await_args.kwargs
    messages = kwargs["messages"]
    assert isinstance(messages, list)
    assert len(messages) == 3
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == user_prompt
    assert messages[1]["role"] == "assistant"
    assert messages[1]["content"] == partial
    assert messages[2]["role"] == "user"
    # The nudge must reference continuing — exact wording can change.
    assert "continu" in messages[2]["content"].lower()


# Editor budget caps — guards against thinking eating the entire output budget.


@pytest.mark.asyncio
async def test_editor_caps_thinking_when_model_config_supplied(mocker):
    """When the bridge supplies a model_config with adaptive thinking
    and no thinking cap, the editor must clone it with max_tokens=64k
    and max_thinking_tokens=48k. That guarantees ≥16k of output text
    even when adaptive thinking maxes out, so the editor can't return
    0-char responses (the failure mode that bit aiep × n_critics_3
    stability re-fires)."""

    fake_text_call = mocker.patch(
        "rumil.orchestrators.draft_and_edit.text_call",
        new=AsyncMock(return_value="<continuation>edited</continuation>"),
    )
    sys_pkg = sys.modules["rumil.orchestrators.draft_and_edit"]
    db = MagicMock()
    db.create_call = AsyncMock(return_value=MagicMock(id="call-1"))
    db.update_call_status = AsyncMock()
    db.save_call = AsyncMock()
    db.save_call_trace = AsyncMock()
    trace = MagicMock()
    trace.record = AsyncMock()
    wf = _make_workflow()

    bridge_config = ModelConfig(
        temperature=None,
        max_tokens=20_000,  # bridge default — much smaller than what editor needs
        thinking={"type": "adaptive"},
        max_thinking_tokens=None,  # uncapped — the failure mode
    )

    await wf._edit(
        db=db,
        trace=trace,
        call_id="call-1",
        round_idx=2,
        prefix="prefix",
        target_length=10_000,
        current_draft="draft",
        critiques=["critic prose"],
        model_config=bridge_config,
    )

    # The editor's text_call should have received a model_config (not
    # discrete max_tokens) carrying the bumped caps.
    fake_text_call.assert_awaited_once()
    kwargs = fake_text_call.await_args.kwargs
    assert "max_tokens" not in kwargs, "should pass via model_config, not discrete arg"
    cfg = kwargs["model_config"]
    assert cfg.max_tokens == 64_000
    assert cfg.max_thinking_tokens == 48_000
    # Editor switches thinking from adaptive (no cap) to enabled (cap-
    # respecting) — the Anthropic API rejects budget_tokens on
    # type=adaptive, so capping requires changing the thinking type.
    assert cfg.thinking == {"type": "enabled"}
    del sys_pkg  # keep ruff quiet


@pytest.mark.asyncio
async def test_editor_uses_discrete_max_tokens_when_no_model_config(mocker):
    """Non-bridge callers don't supply a model_config. The editor
    falls back to the discrete max_tokens arg at the same 64k cap."""
    fake_text_call = mocker.patch(
        "rumil.orchestrators.draft_and_edit.text_call",
        new=AsyncMock(return_value="<continuation>edited</continuation>"),
    )
    db = MagicMock()
    db.create_call = AsyncMock(return_value=MagicMock(id="call-1"))
    db.update_call_status = AsyncMock()
    db.save_call = AsyncMock()
    db.save_call_trace = AsyncMock()
    trace = MagicMock()
    trace.record = AsyncMock()
    wf = _make_workflow()

    await wf._edit(
        db=db,
        trace=trace,
        call_id="call-1",
        round_idx=2,
        prefix="prefix",
        target_length=10_000,
        current_draft="draft",
        critiques=["critic prose"],
        model_config=None,
    )

    fake_text_call.assert_awaited_once()
    kwargs = fake_text_call.await_args.kwargs
    assert kwargs.get("max_tokens") == 64_000
    assert "model_config" not in kwargs or kwargs["model_config"] is None
