"""`_langfuse_input_for` and per-site enrichment fold system_prompt into input.

Langfuse has no dedicated `system` field. We fold it into `input` as
`{"system": ..., "messages": [...]}` so the trace is self-contained and the
playground can replay the call faithfully.
"""

from types import SimpleNamespace

import pytest

from rumil.llm import (
    ParsedAnthropicResponse,
    _enrich_langfuse_generation,
    _enrich_langfuse_generation_google,
    _langfuse_input_for,
)


def test_langfuse_input_for_folds_system_and_serializes_messages():
    payload = _langfuse_input_for("you are helpful", [{"role": "user", "content": "hi"}])

    assert payload == {
        "system": "you are helpful",
        "messages": [{"role": "user", "content": "hi"}],
    }


def test_langfuse_input_for_handles_content_blocks():
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
    ]

    payload = _langfuse_input_for("sys", messages)

    assert payload["system"] == "sys"
    assert payload["messages"][1]["content"] == [{"type": "text", "text": "hello"}]


@pytest.fixture
def langfuse_client(mocker):
    client = mocker.MagicMock()
    client.update_current_generation = mocker.MagicMock()
    mocker.patch("rumil.llm.get_langfuse", return_value=client)
    return client


def _fake_anthropic_response():
    return SimpleNamespace(
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=5,
            output_tokens=2,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )


def test_enrich_langfuse_generation_includes_system_in_input(langfuse_client):
    parsed = ParsedAnthropicResponse(
        text_parts=["ok"], tool_calls=[], thinking=[], redacted_thinking=[]
    )

    _enrich_langfuse_generation(
        model="claude-opus-4-7",
        system_prompt="you are helpful",
        messages=[{"role": "user", "content": "hi"}],
        response=_fake_anthropic_response(),  # pyright: ignore[reportArgumentType]
        elapsed_ms=10,
        parsed=parsed,
        api_kwargs={"model": "claude-opus-4-7", "max_tokens": 100},
    )

    kwargs = langfuse_client.update_current_generation.call_args.kwargs
    assert kwargs["input"] == {
        "system": "you are helpful",
        "messages": [{"role": "user", "content": "hi"}],
    }


def test_enrich_langfuse_generation_google_includes_system_in_input(langfuse_client, mocker):
    mocker.patch("rumil.llm.get_langfuse", return_value=langfuse_client)
    response = mocker.MagicMock()
    response.usage_metadata.prompt_token_count = 5
    response.usage_metadata.candidates_token_count = 2
    response.usage_metadata.cached_content_token_count = 0
    response.text = "ok"

    _enrich_langfuse_generation_google(
        model="gemini-2.5-pro",
        system_prompt="be concise",
        messages=[{"role": "user", "content": "hi"}],
        response=response,
        elapsed_ms=10,
        config={"temperature": 0.0},
    )

    kwargs = langfuse_client.update_current_generation.call_args.kwargs
    assert kwargs["input"] == {
        "system": "be concise",
        "messages": [{"role": "user", "content": "hi"}],
    }
