"""Unit tests for `parse_anthropic_response` and `ParsedAnthropicResponse`.

Pure parsing logic — no DB or LLM access. Constructs real
`anthropic.types.*Block` instances so the `isinstance` checks inside the
parser are exercised against the same classes a live response would
yield.
"""

from anthropic.types import (
    RedactedThinkingBlock,
    ServerToolUseBlock,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
)

from rumil.llm import ParsedAnthropicResponse, parse_anthropic_response


def _text(text: str) -> TextBlock:
    return TextBlock(type="text", text=text, citations=None)


def _thinking(content: str, signature: str = "sig") -> ThinkingBlock:
    return ThinkingBlock(type="thinking", thinking=content, signature=signature)


def _redacted(data: str) -> RedactedThinkingBlock:
    return RedactedThinkingBlock(type="redacted_thinking", data=data)


def _tool_use(name: str, payload: dict) -> ToolUseBlock:
    return ToolUseBlock(type="tool_use", id="toolu_1", name=name, input=payload)


def _server_tool_use(payload: dict) -> ServerToolUseBlock:
    # `name` is constrained to a Literal of known server tools — `web_search`
    # is the obvious one to test against.
    return ServerToolUseBlock(type="server_tool_use", id="srv_1", name="web_search", input=payload)


def test_text_only_response_has_no_thinking():
    parsed = parse_anthropic_response([_text("hello world")])

    assert parsed.text_parts == ["hello world"]
    assert parsed.text == "hello world"
    assert parsed.tool_calls == []
    assert parsed.thinking == []
    assert parsed.redacted_thinking == []
    assert parsed.has_thinking is False
    assert parsed.thinking_blocks_for_storage() is None


def test_multiple_text_blocks_are_joined_with_newline():
    parsed = parse_anthropic_response([_text("line one"), _text("line two")])

    assert parsed.text_parts == ["line one", "line two"]
    assert parsed.text == "line one\nline two"


def test_thinking_block_is_extracted_with_signature():
    parsed = parse_anthropic_response(
        [_thinking("step-by-step CoT", signature="abc"), _text("the answer")]
    )

    assert parsed.text == "the answer"
    assert parsed.thinking == [{"content": "step-by-step CoT", "signature": "abc"}]
    assert parsed.has_thinking is True
    assert parsed.thinking_blocks_for_storage() == {
        "thinking": [{"content": "step-by-step CoT", "signature": "abc"}],
    }


def test_thinking_with_empty_content_still_records_block():
    # Opus 4.7 default behavior (display: omitted) — block is present but the
    # `thinking` text is empty. We still want to record it so callers can tell
    # "model used thinking but didn't disclose" from "model didn't think".
    parsed = parse_anthropic_response([_thinking("", signature="sig"), _text("answer")])

    assert parsed.has_thinking is True
    assert parsed.thinking == [{"content": "", "signature": "sig"}]
    storage = parsed.thinking_blocks_for_storage()
    assert storage is not None
    assert storage["thinking"][0]["content"] == ""


def test_redacted_thinking_block_is_extracted():
    parsed = parse_anthropic_response([_redacted("encrypted-blob"), _text("user-facing answer")])

    assert parsed.redacted_thinking == [{"data": "encrypted-blob"}]
    assert parsed.thinking == []
    assert parsed.has_thinking is True
    assert parsed.thinking_blocks_for_storage() == {
        "redacted_thinking": [{"data": "encrypted-blob"}],
    }


def test_thinking_and_redacted_coexist_in_storage_payload():
    parsed = parse_anthropic_response(
        [
            _thinking("visible CoT", signature="s1"),
            _redacted("opaque"),
            _text("answer"),
        ]
    )

    storage = parsed.thinking_blocks_for_storage()
    assert storage == {
        "thinking": [{"content": "visible CoT", "signature": "s1"}],
        "redacted_thinking": [{"data": "opaque"}],
    }


def test_tool_use_blocks_are_collected_alongside_thinking():
    parsed = parse_anthropic_response(
        [
            _thinking("plan: call the tool"),
            _tool_use("search", {"q": "rumil"}),
            _server_tool_use({"q": "claude"}),
        ]
    )

    assert parsed.tool_calls == [
        {"name": "search", "input": {"q": "rumil"}},
        {"name": "web_search", "input": {"q": "claude"}},
    ]
    assert parsed.thinking == [{"content": "plan: call the tool", "signature": "sig"}]
    assert parsed.text == ""


def test_empty_content_returns_empty_parsed_response():
    parsed = parse_anthropic_response([])

    assert isinstance(parsed, ParsedAnthropicResponse)
    assert parsed.text_parts == []
    assert parsed.text == ""
    assert parsed.tool_calls == []
    assert parsed.has_thinking is False
    assert parsed.thinking_blocks_for_storage() is None
