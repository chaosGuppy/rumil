"""Full request/response bodies and provider_request_id on call_llm_exchanges.

Covers the changes from #463: ``_serialize_request_for_storage`` rewrites
Pydantic classes to a JSONB-safe shape, and the API/persistence path forwards
``request``/``response``/``provider_request_id`` to ``db.save_llm_exchange``.
"""

import json
from typing import Any

import pytest
from anthropic.types import ThinkingBlock
from pydantic import BaseModel

from rumil.llm import (
    LLMExchangeMetadata,
    _record_partial_failure,
    _save_exchange,
    _serialize_request_for_storage,
    call_anthropic_api,
)


class _Schema(BaseModel):
    item_id: str
    score: int


@pytest.fixture
def metadata() -> LLMExchangeMetadata:
    return LLMExchangeMetadata(call_id="call_123", phase="test_phase", round_num=0)


@pytest.fixture
def db_mock(mocker):
    db = mocker.MagicMock()
    db.save_llm_exchange = mocker.AsyncMock(return_value="exchange_abc")
    return db


@pytest.fixture
def trace_mock(mocker):
    trace = mocker.MagicMock()
    trace.record = mocker.AsyncMock()
    mocker.patch("rumil.llm.get_trace", return_value=trace)
    return trace


@pytest.fixture
def langfuse_url(mocker):
    mocker.patch("rumil.llm.langfuse_trace_url_for_current_observation", return_value=None)


@pytest.fixture
def no_langfuse(mocker):
    mocker.patch("rumil.llm.get_langfuse", return_value=None)


def test_serialize_rewrites_pydantic_output_format():
    out = _serialize_request_for_storage({"model": "claude", "output_format": _Schema})

    assert out["output_format"]["name"] == "_Schema"
    assert out["output_format"]["schema"] == _Schema.model_json_schema()


def test_serialize_passes_through_without_output_format():
    kwargs = {"model": "claude", "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}]}

    out = _serialize_request_for_storage(kwargs)

    assert out == kwargs


def test_serialize_passes_through_when_output_format_is_none():
    out = _serialize_request_for_storage({"model": "claude", "output_format": None})

    assert out["output_format"] is None


def test_serialize_does_not_mutate_input():
    original = {"model": "claude", "output_format": _Schema}

    _serialize_request_for_storage(original)

    assert original["output_format"] is _Schema


def test_serialize_flattens_sdk_thinking_blocks_in_messages():
    """Assistant turns echoed back to the API carry ``ThinkingBlock`` SDK objects
    on extended-thinking models. The serialized form must be JSON-encodable so
    Supabase can write the ``request`` JSONB column."""
    thinking = ThinkingBlock(type="thinking", thinking="reasoning…", signature="sig")
    kwargs = {
        "model": "claude-opus-4-7",
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [thinking, {"type": "text", "text": "ok"}]},
        ],
    }

    out = _serialize_request_for_storage(kwargs)

    json.dumps(out)
    assistant_blocks = out["messages"][1]["content"]
    assert assistant_blocks[0]["type"] == "thinking"
    assert assistant_blocks[0]["thinking"] == "reasoning…"
    assert assistant_blocks[1] == {"type": "text", "text": "ok"}


def test_serialize_preserves_other_kwargs_alongside_rewrite():
    out = _serialize_request_for_storage(
        {
            "model": "claude-opus-4-7",
            "max_tokens": 200,
            "messages": [{"role": "user", "content": "hi"}],
            "output_format": _Schema,
        }
    )

    assert out["model"] == "claude-opus-4-7"
    assert out["max_tokens"] == 200
    assert out["messages"] == [{"role": "user", "content": "hi"}]
    assert isinstance(out["output_format"], dict)


@pytest.mark.asyncio
async def test_save_exchange_forwards_request_response_and_provider_id(
    metadata, db_mock, trace_mock, langfuse_url
):
    request = {"model": "claude-opus-4-7", "max_tokens": 100}
    response = {"id": "msg_01abc", "content": [{"type": "text", "text": "hi"}]}

    await _save_exchange(
        metadata,
        db=db_mock,
        model="claude-opus-4-7",
        system_prompt="sys",
        response_text="hi",
        tool_calls=[],
        input_tokens=1,
        output_tokens=1,
        duration_ms=1,
        request=request,
        response=response,
        provider_request_id="req_01XYZ",
    )

    kwargs = db_mock.save_llm_exchange.await_args.kwargs
    assert kwargs["request"] == request
    assert kwargs["response"] == response
    assert kwargs["provider_request_id"] == "req_01XYZ"


@pytest.mark.asyncio
async def test_save_exchange_defaults_new_fields_to_none(
    metadata, db_mock, trace_mock, langfuse_url
):
    await _save_exchange(
        metadata,
        db=db_mock,
        model="claude-haiku-4-5",
        system_prompt="sys",
        response_text="hi",
        tool_calls=[],
        input_tokens=1,
        output_tokens=1,
        duration_ms=1,
    )

    kwargs = db_mock.save_llm_exchange.await_args.kwargs
    assert kwargs["request"] is None
    assert kwargs["response"] is None
    assert kwargs["provider_request_id"] is None


@pytest.mark.asyncio
async def test_record_partial_failure_forwards_request_and_provider_id(
    metadata, db_mock, trace_mock, langfuse_url, no_langfuse
):
    await _record_partial_failure(
        exc=RuntimeError("boom"),
        partial_text="partial",
        partial_tool_calls=None,
        metadata=metadata,
        db=db_mock,
        model="claude-opus-4-7",
        system_prompt="sys",
        messages=[{"role": "user", "content": "hi"}],
        elapsed_ms=10,
        request={"model": "claude-opus-4-7", "max_tokens": 50},
        provider_request_id="req_partialXYZ",
    )

    kwargs = db_mock.save_llm_exchange.await_args.kwargs
    assert kwargs["request"] == {"model": "claude-opus-4-7", "max_tokens": 50}
    assert kwargs["provider_request_id"] == "req_partialXYZ"
    assert kwargs["error"].startswith("RuntimeError")


def _make_stream_cm(
    *,
    response: Any | None,
    request_id: str | None,
    partial_blocks: Any | None = None,
    snapshot_raises: bool = False,
    mocker,
):
    """Async-context-manager mock for ``client.messages.stream``.

    Pass ``response=None`` to make the stream raise on get_final_message;
    pass a response object to make it succeed.
    """
    stream = mocker.MagicMock()
    stream.request_id = request_id
    if response is None:
        stream.get_final_message = mocker.AsyncMock(side_effect=RuntimeError("stream broke"))
        if snapshot_raises:
            type(stream).current_message_snapshot = mocker.PropertyMock(
                side_effect=AssertionError("no message_start yet")
            )
        else:
            snapshot = mocker.MagicMock()
            snapshot.content = partial_blocks or []
            stream.current_message_snapshot = snapshot
    else:
        stream.get_final_message = mocker.AsyncMock(return_value=response)

    cm = mocker.MagicMock()
    cm.__aenter__ = mocker.AsyncMock(return_value=stream)
    cm.__aexit__ = mocker.AsyncMock(return_value=False)
    return cm


def _make_response_mock(mocker, *, response_dict: dict) -> Any:
    """Build a Message-like mock that parse_anthropic_response can consume."""
    from anthropic.types import TextBlock

    response = mocker.MagicMock()
    response.content = [TextBlock(type="text", text="hi")]
    response.usage.input_tokens = 5
    response.usage.output_tokens = 7
    response.usage.cache_creation_input_tokens = 0
    response.usage.cache_read_input_tokens = 0
    response.model_dump = mocker.MagicMock(return_value=response_dict)
    return response


@pytest.mark.asyncio
async def test_call_anthropic_api_success_persists_request_response_and_provider_id(
    metadata, db_mock, trace_mock, langfuse_url, no_langfuse, mocker
):
    response_dict = {"id": "msg_01success", "content": [{"type": "text", "text": "hi"}]}
    response = _make_response_mock(mocker, response_dict=response_dict)
    cm = _make_stream_cm(response=response, request_id="req_01SUCCESS", mocker=mocker)
    client = mocker.MagicMock()
    client.messages.stream = mocker.MagicMock(return_value=cm)

    await call_anthropic_api(
        client,
        model="claude-opus-4-7",
        system_prompt="sys",
        messages=[{"role": "user", "content": "hi"}],
        metadata=metadata,
        db=db_mock,
    )

    kwargs = db_mock.save_llm_exchange.await_args.kwargs
    assert kwargs["provider_request_id"] == "req_01SUCCESS"
    assert kwargs["response"] == response_dict
    # Request snapshot includes the user message and model that went on the wire.
    assert kwargs["request"]["model"] == "claude-opus-4-7"
    assert kwargs["request"]["messages"] == [{"role": "user", "content": "hi"}]


@pytest.mark.asyncio
async def test_call_anthropic_api_partial_failure_captures_stream_request_id(
    metadata, db_mock, trace_mock, langfuse_url, no_langfuse, mocker
):
    from anthropic.types import TextBlock

    text_block = TextBlock(type="text", text="partial assistant text")
    cm = _make_stream_cm(
        response=None,
        request_id="req_01PARTIAL",
        partial_blocks=[text_block],
        mocker=mocker,
    )
    client = mocker.MagicMock()
    client.messages.stream = mocker.MagicMock(return_value=cm)

    with pytest.raises(RuntimeError, match="stream broke"):
        await call_anthropic_api(
            client,
            model="claude-opus-4-7",
            system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
            metadata=metadata,
            db=db_mock,
        )

    kwargs = db_mock.save_llm_exchange.await_args.kwargs
    assert kwargs["provider_request_id"] == "req_01PARTIAL"
    assert kwargs["request"]["model"] == "claude-opus-4-7"
    # Response column stays NULL on failure — we never got a final Message.
    assert kwargs["response"] is None
