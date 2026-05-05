"""Forensics for failed LLM calls (#446).

Covers the partial-response recovery helpers in ``rumil.llm`` and the
failure paths of ``_structured_call_parse`` / ``call_anthropic_api``.
"""

from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from rumil.llm import (
    LLMExchangeMetadata,
    _partial_from_validation_error,
    _record_partial_failure,
    _structured_call_parse,
    call_anthropic_api,
)


class _Schema(BaseModel):
    item_reviews: list


def _make_validation_error(text: str) -> ValidationError:
    try:
        _Schema.model_validate_json(text)
    except ValidationError as exc:
        return exc
    raise AssertionError("expected ValidationError")


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
def langfuse_mock(mocker):
    client = mocker.MagicMock()
    client.update_current_generation = mocker.MagicMock()
    mocker.patch("rumil.llm.get_langfuse", return_value=client)
    return client


@pytest.fixture
def no_langfuse(mocker):
    mocker.patch("rumil.llm.get_langfuse", return_value=None)


def test_partial_from_validation_error_returns_full_input():
    big = '{"item_reviews":[{"item_id":"a","reasoning":"' + ("blah " * 6000) + "...END"
    exc = _make_validation_error(big)

    result = _partial_from_validation_error(exc)

    assert isinstance(result, str)
    assert result == big
    assert len(result) > 30_000


def test_partial_from_validation_error_returns_none_for_non_string_input(mocker):
    exc = mocker.MagicMock(spec=ValidationError)
    exc.errors = mocker.MagicMock(return_value=[{"type": "missing", "input": 42}])

    assert _partial_from_validation_error(exc) is None


def test_partial_from_validation_error_returns_none_when_errors_empty(mocker):
    exc = mocker.MagicMock(spec=ValidationError)
    exc.errors = mocker.MagicMock(return_value=[])

    assert _partial_from_validation_error(exc) is None


def test_partial_from_validation_error_returns_none_when_input_missing(mocker):
    exc = mocker.MagicMock(spec=ValidationError)
    exc.errors = mocker.MagicMock(return_value=[{"type": "missing"}])

    assert _partial_from_validation_error(exc) is None


@pytest.mark.asyncio
async def test_record_partial_failure_writes_exchange_with_error_and_partial(
    metadata, db_mock, trace_mock, langfuse_url, langfuse_mock
):
    exc = RuntimeError("collapse")

    await _record_partial_failure(
        exc=exc,
        partial_text="partial output",
        partial_tool_calls=None,
        metadata=metadata,
        db=db_mock,
        model="claude-opus-4-7",
        system_prompt="sys",
        messages=[{"role": "user", "content": "hi"}],
        elapsed_ms=42,
    )

    db_mock.save_llm_exchange.assert_awaited_once()
    kwargs = db_mock.save_llm_exchange.await_args.kwargs
    assert kwargs["response_text"] == "partial output"
    assert kwargs["error"].startswith("RuntimeError: collapse")
    assert kwargs["duration_ms"] == 42


@pytest.mark.asyncio
async def test_record_partial_failure_enriches_langfuse_with_partial_output(
    metadata, db_mock, trace_mock, langfuse_url, langfuse_mock
):
    await _record_partial_failure(
        exc=RuntimeError("boom"),
        partial_text="partial output",
        partial_tool_calls=None,
        metadata=metadata,
        db=db_mock,
        model="claude-opus-4-7",
        system_prompt="sys",
        messages=[{"role": "user", "content": "hi"}],
        elapsed_ms=10,
    )

    langfuse_mock.update_current_generation.assert_called_once_with(output="partial output")


@pytest.mark.asyncio
async def test_record_partial_failure_skips_langfuse_when_no_partial_text(
    metadata, db_mock, trace_mock, langfuse_url, langfuse_mock
):
    await _record_partial_failure(
        exc=RuntimeError("boom"),
        partial_text=None,
        partial_tool_calls=None,
        metadata=metadata,
        db=db_mock,
        model="claude-opus-4-7",
        system_prompt="sys",
        messages=[{"role": "user", "content": "hi"}],
        elapsed_ms=10,
    )

    langfuse_mock.update_current_generation.assert_not_called()


@pytest.mark.asyncio
async def test_record_partial_failure_no_op_without_metadata_or_db(no_langfuse):
    await _record_partial_failure(
        exc=RuntimeError("boom"),
        partial_text="partial",
        partial_tool_calls=None,
        metadata=None,
        db=None,
        model="claude-opus-4-7",
        system_prompt="sys",
        messages=[{"role": "user", "content": "hi"}],
        elapsed_ms=10,
    )


@pytest.mark.asyncio
async def test_record_partial_failure_swallows_save_exchange_errors(
    metadata, trace_mock, langfuse_url, no_langfuse, mocker
):
    db = mocker.MagicMock()
    db.save_llm_exchange = mocker.AsyncMock(side_effect=Exception("db down"))

    await _record_partial_failure(
        exc=RuntimeError("boom"),
        partial_text=None,
        partial_tool_calls=None,
        metadata=metadata,
        db=db,
        model="claude-opus-4-7",
        system_prompt="sys",
        messages=[{"role": "user", "content": "hi"}],
        elapsed_ms=10,
    )


@pytest.mark.asyncio
async def test_record_partial_failure_swallows_langfuse_errors(
    metadata, db_mock, trace_mock, langfuse_url, mocker
):
    client = mocker.MagicMock()
    client.update_current_generation = mocker.MagicMock(side_effect=Exception("lf down"))
    mocker.patch("rumil.llm.get_langfuse", return_value=client)

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
    )


@pytest.fixture
def patch_anthropic_client(mocker):
    """Replace ``anthropic.AsyncAnthropic(...)`` in rumil.llm with a configurable mock client."""
    client = mocker.MagicMock()
    mocker.patch("rumil.llm.anthropic.AsyncAnthropic", return_value=client)
    from rumil.settings import Settings

    mocker.patch.object(Settings, "require_anthropic_key", lambda self: "test-key")
    return client


@pytest.mark.asyncio
async def test_structured_call_parse_records_partial_on_validation_error(
    metadata, db_mock, trace_mock, langfuse_url, no_langfuse, patch_anthropic_client, mocker
):
    big = '{"item_reviews":[{"item_id":"a","reasoning":"' + ("X" * 30_000) + "...END"
    parse_exc = _make_validation_error(big)
    patch_anthropic_client.messages.parse = mocker.AsyncMock(side_effect=parse_exc)

    with pytest.raises(ValidationError):
        await _structured_call_parse(
            "system",
            _Schema,
            [{"role": "user", "content": "hi"}],
            metadata=metadata,
            db=db_mock,
            model="claude-opus-4-7",
        )

    db_mock.save_llm_exchange.assert_awaited_once()
    kwargs = db_mock.save_llm_exchange.await_args.kwargs
    assert kwargs["response_text"] == big
    assert "ValidationError" in kwargs["error"]


@pytest.mark.asyncio
async def test_structured_call_parse_records_failure_on_non_validation_exception(
    metadata, db_mock, trace_mock, langfuse_url, no_langfuse, patch_anthropic_client, mocker
):
    patch_anthropic_client.messages.parse = mocker.AsyncMock(side_effect=RuntimeError("net"))

    with pytest.raises(RuntimeError, match="net"):
        await _structured_call_parse(
            "system",
            _Schema,
            [{"role": "user", "content": "hi"}],
            metadata=metadata,
            db=db_mock,
            model="claude-opus-4-7",
        )

    db_mock.save_llm_exchange.assert_awaited_once()
    kwargs = db_mock.save_llm_exchange.await_args.kwargs
    assert kwargs["response_text"] is None
    assert "RuntimeError" in kwargs["error"]


def _make_stream_cm(*, partial_blocks: Any | None, snapshot_raises: bool, mocker):
    """Build an async-context-manager mock matching ``client.messages.stream``."""
    stream = mocker.MagicMock()
    stream.get_final_message = mocker.AsyncMock(side_effect=RuntimeError("stream broke"))
    if snapshot_raises:
        type(stream).current_message_snapshot = mocker.PropertyMock(
            side_effect=AssertionError("no message_start yet")
        )
    else:
        snapshot = mocker.MagicMock()
        snapshot.content = partial_blocks or []
        stream.current_message_snapshot = snapshot

    cm = mocker.MagicMock()
    cm.__aenter__ = mocker.AsyncMock(return_value=stream)
    cm.__aexit__ = mocker.AsyncMock(return_value=False)
    return cm


@pytest.mark.asyncio
async def test_call_anthropic_api_records_partial_text_from_stream_snapshot(
    metadata, db_mock, trace_mock, langfuse_url, no_langfuse, mocker
):
    from anthropic.types import TextBlock

    text_block = TextBlock(type="text", text="partial assistant text")
    cm = _make_stream_cm(partial_blocks=[text_block], snapshot_raises=False, mocker=mocker)
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

    db_mock.save_llm_exchange.assert_awaited_once()
    kwargs = db_mock.save_llm_exchange.await_args.kwargs
    assert kwargs["response_text"] == "partial assistant text"
    assert "RuntimeError" in kwargs["error"]


@pytest.mark.asyncio
async def test_structured_call_parse_request_kwargs_are_json_serializable(
    metadata, db_mock, trace_mock, langfuse_url, no_langfuse, patch_anthropic_client, mocker
):
    import json

    big = '{"item_reviews":[{"item_id":"a","reasoning":"' + ("X" * 100) + "...END"
    parse_exc = _make_validation_error(big)
    patch_anthropic_client.messages.parse = mocker.AsyncMock(side_effect=parse_exc)

    with pytest.raises(ValidationError):
        await _structured_call_parse(
            "system",
            _Schema,
            [{"role": "user", "content": "hi"}],
            metadata=metadata,
            db=db_mock,
            model="claude-opus-4-7",
        )

    db_mock.save_llm_exchange.assert_awaited_once()
    kwargs = db_mock.save_llm_exchange.await_args.kwargs
    json.dumps(kwargs.get("request_kwargs"))


@pytest.mark.asyncio
async def test_call_anthropic_api_records_failure_when_snapshot_unavailable(
    metadata, db_mock, trace_mock, langfuse_url, no_langfuse, mocker
):
    cm = _make_stream_cm(partial_blocks=None, snapshot_raises=True, mocker=mocker)
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

    db_mock.save_llm_exchange.assert_awaited_once()
    kwargs = db_mock.save_llm_exchange.await_args.kwargs
    assert kwargs["response_text"] is None
    assert "RuntimeError" in kwargs["error"]
