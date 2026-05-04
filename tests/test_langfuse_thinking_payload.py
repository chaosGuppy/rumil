"""`_langfuse_output_for` produces the assistant-message shape Langfuse renders.

Langfuse's IOPreview renders a collapsible "Thinking" section when the
assistant message has a `thinking` array of `{content, summary?}`
entries (and a "Thinking (redacted)" section for `redacted_thinking`
entries with `data`). We must NOT include the Anthropic `signature` —
it's opaque to the UI and is persisted in our own DB JSONB column
instead.
"""

from rumil.llm import ParsedAnthropicResponse, _langfuse_output_for


def _parsed(
    text_parts: list[str] | None = None,
    thinking: list[dict] | None = None,
    redacted_thinking: list[dict] | None = None,
) -> ParsedAnthropicResponse:
    return ParsedAnthropicResponse(
        text_parts=text_parts or [],
        tool_calls=[],
        thinking=thinking or [],
        redacted_thinking=redacted_thinking or [],
    )


def test_no_thinking_returns_text_string():
    out = _langfuse_output_for(_parsed(text_parts=["plain answer"]))
    assert out == "plain answer"


def test_no_thinking_and_no_text_returns_none():
    assert _langfuse_output_for(_parsed()) is None


def test_thinking_present_returns_assistant_message_dict():
    parsed = _parsed(
        text_parts=["the answer"],
        thinking=[{"content": "step-by-step CoT", "signature": "abc"}],
    )

    out = _langfuse_output_for(parsed)

    assert out == {
        "role": "assistant",
        "content": "the answer",
        "thinking": [{"content": "step-by-step CoT"}],
    }


def test_signature_is_stripped_from_langfuse_payload():
    """Signature stays in our DB column but does not leak to Langfuse."""
    parsed = _parsed(
        text_parts=["a"],
        thinking=[
            {"content": "block 1", "signature": "sig-1"},
            {"content": "block 2", "signature": "sig-2"},
        ],
    )

    out = _langfuse_output_for(parsed)

    assert isinstance(out, dict)
    for block in out["thinking"]:
        assert "signature" not in block


def test_redacted_only_returns_assistant_message_dict():
    parsed = _parsed(
        text_parts=["safe answer"],
        redacted_thinking=[{"data": "opaque-blob"}],
    )

    out = _langfuse_output_for(parsed)

    assert out == {
        "role": "assistant",
        "content": "safe answer",
        "redacted_thinking": [{"data": "opaque-blob"}],
    }


def test_thinking_and_redacted_both_present():
    parsed = _parsed(
        text_parts=["answer"],
        thinking=[{"content": "visible CoT", "signature": "s"}],
        redacted_thinking=[{"data": "opaque"}],
    )

    out = _langfuse_output_for(parsed)

    assert out == {
        "role": "assistant",
        "content": "answer",
        "thinking": [{"content": "visible CoT"}],
        "redacted_thinking": [{"data": "opaque"}],
    }


def test_thinking_with_empty_text_still_emits_dict():
    # Some agentic turns end with a thinking block and no user-facing text.
    parsed = _parsed(thinking=[{"content": "internal-only", "signature": "s"}])

    out = _langfuse_output_for(parsed)

    assert out == {
        "role": "assistant",
        "content": "",
        "thinking": [{"content": "internal-only"}],
    }
