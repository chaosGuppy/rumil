"""`_langfuse_input_for` and per-site enrichment fold system_prompt into input.

Langfuse has no dedicated `system` field. We prepend a `{"role": "system", ...}`
entry to a flat ChatML list so the trace UI shows the system message at the
top and "Open in Playground" pre-fills it correctly.
"""

from types import SimpleNamespace

import pytest

from rumil.llm import (
    ParsedAnthropicResponse,
    _enrich_langfuse_generation,
    _enrich_langfuse_generation_google,
    _langfuse_input_for,
)


def test_langfuse_input_for_prepends_system_as_chatml_message():
    payload = _langfuse_input_for("you are helpful", [{"role": "user", "content": "hi"}])

    assert payload == [
        {"role": "system", "content": "you are helpful"},
        {"role": "user", "content": "hi"},
    ]


def test_langfuse_input_for_omits_system_when_empty():
    payload = _langfuse_input_for("", [{"role": "user", "content": "hi"}])

    assert payload == [{"role": "user", "content": "hi"}]


def test_langfuse_input_for_handles_content_blocks():
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
    ]

    payload = _langfuse_input_for("sys", messages)

    assert payload[0] == {"role": "system", "content": "sys"}
    assert payload[2]["content"] == [{"type": "text", "text": "hello"}]


def test_langfuse_input_for_wraps_messages_when_tools_present():
    tools = [{"name": "search", "description": "search", "input_schema": {"type": "object"}}]

    payload = _langfuse_input_for(
        "you are helpful", [{"role": "user", "content": "hi"}], tools=tools
    )

    # Anthropic-shape tools translate to OpenAI shape so Langfuse's IOPreview
    # renders with its tool-definition UI rather than dumping raw JSON.
    assert payload == {
        "messages": [
            {"role": "system", "content": "you are helpful"},
            {"role": "user", "content": "hi"},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "search",
                    "description": "search",
                    "parameters": {"type": "object"},
                },
            }
        ],
    }


def test_langfuse_input_for_returns_flat_list_when_tools_empty():
    payload = _langfuse_input_for("you are helpful", [{"role": "user", "content": "hi"}], tools=[])

    assert payload == [
        {"role": "system", "content": "you are helpful"},
        {"role": "user", "content": "hi"},
    ]


def test_langfuse_input_for_includes_response_format_when_schema_present():
    schema = {
        "name": "MyModel",
        "schema": {"type": "object", "properties": {"x": {"type": "string"}}},
    }

    payload = _langfuse_input_for(
        "sys", [{"role": "user", "content": "hi"}], response_schema=schema
    )

    # Schema-only (no tools) still wraps as a dict so the response_format field surfaces.
    assert payload == {
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "MyModel",
                "schema": {"type": "object", "properties": {"x": {"type": "string"}}},
                "strict": True,
            },
        },
    }


def test_langfuse_input_for_combines_tools_and_schema():
    tools = [{"name": "noop", "description": "", "input_schema": {"type": "object"}}]
    schema = {"name": "M", "schema": {"type": "object"}}

    payload = _langfuse_input_for(
        "sys",
        [{"role": "user", "content": "hi"}],
        tools=tools,
        response_schema=schema,
    )

    assert isinstance(payload, dict)
    assert "tools" in payload and "response_format" in payload
    assert payload["response_format"]["json_schema"]["name"] == "M"


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
    assert kwargs["input"] == [
        {"role": "system", "content": "you are helpful"},
        {"role": "user", "content": "hi"},
    ]


def test_enrich_langfuse_generation_includes_tools_in_input(langfuse_client):
    parsed = ParsedAnthropicResponse(
        text_parts=["ok"], tool_calls=[], thinking=[], redacted_thinking=[]
    )
    tools = [{"name": "search", "description": "search", "input_schema": {"type": "object"}}]

    _enrich_langfuse_generation(
        model="claude-opus-4-7",
        system_prompt="you are helpful",
        messages=[{"role": "user", "content": "hi"}],
        response=_fake_anthropic_response(),  # pyright: ignore[reportArgumentType]
        elapsed_ms=10,
        parsed=parsed,
        api_kwargs={"model": "claude-opus-4-7", "max_tokens": 100, "tools": tools},
    )

    kwargs = langfuse_client.update_current_generation.call_args.kwargs
    assert kwargs["input"] == {
        "messages": [
            {"role": "system", "content": "you are helpful"},
            {"role": "user", "content": "hi"},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "search",
                    "description": "search",
                    "parameters": {"type": "object"},
                },
            }
        ],
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
    assert kwargs["input"] == [
        {"role": "system", "content": "be concise"},
        {"role": "user", "content": "hi"},
    ]
