"""Continuation-style recovery for cut-off structured-output JSON (#444).

Covers ``_continuation_recovery_for_parse`` and the
``continuation_recovery=True`` opt-in on ``_structured_call_parse``.
"""

import pytest
from anthropic.types import TextBlock
from pydantic import BaseModel, ValidationError

from rumil.llm import (
    APIResponse,
    LLMExchangeMetadata,
    _continuation_recovery_for_parse,
    _structured_call_parse,
    derive_model_config,
)


class _Schema(BaseModel):
    item_reviews: list


def _make_validation_error(text: str) -> ValidationError:
    try:
        _Schema.model_validate_json(text)
    except ValidationError as exc:
        return exc
    raise AssertionError("expected ValidationError")


def _api_response(text: str) -> APIResponse:
    msg = type(
        "FakeMessage",
        (),
        {"content": [TextBlock(type="text", text=text)]},
    )()
    return APIResponse(message=msg, duration_ms=10)  # type: ignore[arg-type]


@pytest.fixture
def metadata() -> LLMExchangeMetadata:
    return LLMExchangeMetadata(call_id="call_123", phase="deep_review_batch_0", round_num=0)


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


@pytest.fixture
def anthropic_test_key(mocker):
    from rumil.settings import Settings

    mocker.patch.object(Settings, "require_anthropic_key", lambda self: "test-key")
    mocker.patch("rumil.llm.anthropic.AsyncAnthropic", return_value=mocker.MagicMock())


@pytest.mark.asyncio
async def test_continuation_recovery_completes_truncated_json(
    metadata, db_mock, trace_mock, langfuse_url, no_langfuse, anthropic_test_key, mocker
):
    partial = '{"item_reviews":[{"item_id":"a","reasoning":"hello'
    continuation = '"}]}'
    call_api = mocker.patch(
        "rumil.llm.call_anthropic_api",
        new=mocker.AsyncMock(return_value=_api_response(continuation)),
    )

    result = await _continuation_recovery_for_parse(
        response_model=_Schema,
        system_prompt="sys",
        msg_list=[{"role": "user", "content": "hi"}],
        partial_text=partial,
        model="claude-opus-4-7",
        cfg=derive_model_config("claude-opus-4-7"),
        cache=False,
        metadata=metadata,
        db=db_mock,
    )

    assert result is not None
    parsed, full = result
    assert isinstance(parsed, _Schema)
    assert full == partial + continuation
    assert call_api.await_count == 1


@pytest.mark.asyncio
async def test_continuation_recovery_retries_until_success(
    metadata, db_mock, trace_mock, langfuse_url, no_langfuse, anthropic_test_key, mocker
):
    partial = '{"item_reviews":[{"item_id":"a","reasoning":"hello'
    call_api = mocker.patch(
        "rumil.llm.call_anthropic_api",
        new=mocker.AsyncMock(
            side_effect=[
                _api_response(" more text"),
                _api_response('"}]}'),
            ]
        ),
    )

    result = await _continuation_recovery_for_parse(
        response_model=_Schema,
        system_prompt="sys",
        msg_list=[{"role": "user", "content": "hi"}],
        partial_text=partial,
        model="claude-opus-4-7",
        cfg=derive_model_config("claude-opus-4-7"),
        cache=False,
        metadata=metadata,
        db=db_mock,
        max_attempts=2,
    )

    assert result is not None
    parsed, full = result
    assert isinstance(parsed, _Schema)
    assert full == partial + ' more text"}]}'
    assert call_api.await_count == 2


@pytest.mark.asyncio
async def test_continuation_recovery_returns_none_on_exhaustion(
    metadata, db_mock, trace_mock, langfuse_url, no_langfuse, anthropic_test_key, mocker
):
    partial = '{"item_reviews":[{"item_id":"a","reasoning":"hello'
    call_api = mocker.patch(
        "rumil.llm.call_anthropic_api",
        new=mocker.AsyncMock(
            return_value=_api_response(" still no closing brackets"),
        ),
    )

    result = await _continuation_recovery_for_parse(
        response_model=_Schema,
        system_prompt="sys",
        msg_list=[{"role": "user", "content": "hi"}],
        partial_text=partial,
        model="claude-opus-4-7",
        cfg=derive_model_config("claude-opus-4-7"),
        cache=False,
        metadata=metadata,
        db=db_mock,
        max_attempts=2,
    )

    assert result is None
    assert call_api.await_count == 2


@pytest.mark.asyncio
async def test_continuation_recovery_aborts_on_continuation_exception(
    metadata, db_mock, trace_mock, langfuse_url, no_langfuse, anthropic_test_key, mocker
):
    partial = '{"item_reviews":[{"item_id":"a","reasoning":"hello'
    call_api = mocker.patch(
        "rumil.llm.call_anthropic_api",
        new=mocker.AsyncMock(side_effect=RuntimeError("net down")),
    )

    result = await _continuation_recovery_for_parse(
        response_model=_Schema,
        system_prompt="sys",
        msg_list=[{"role": "user", "content": "hi"}],
        partial_text=partial,
        model="claude-opus-4-7",
        cfg=derive_model_config("claude-opus-4-7"),
        cache=False,
        metadata=metadata,
        db=db_mock,
        max_attempts=2,
    )

    assert result is None
    assert call_api.await_count == 1


@pytest.fixture
def patch_anthropic_client(mocker):
    client = mocker.MagicMock()
    mocker.patch("rumil.llm.anthropic.AsyncAnthropic", return_value=client)
    from rumil.settings import Settings

    mocker.patch.object(Settings, "require_anthropic_key", lambda self: "test-key")
    return client


@pytest.mark.asyncio
async def test_structured_call_parse_propagates_when_recovery_disabled(
    metadata, db_mock, trace_mock, langfuse_url, no_langfuse, patch_anthropic_client, mocker
):
    partial = '{"item_reviews":[{"item_id":"a","reasoning":"hello'
    parse_exc = _make_validation_error(partial)
    patch_anthropic_client.messages.parse = mocker.AsyncMock(side_effect=parse_exc)
    call_api = mocker.patch("rumil.llm.call_anthropic_api", new=mocker.AsyncMock())

    with pytest.raises(ValidationError):
        await _structured_call_parse(
            "system",
            _Schema,
            [{"role": "user", "content": "hi"}],
            metadata=metadata,
            db=db_mock,
            model="claude-opus-4-7",
            continuation_recovery=False,
        )

    assert call_api.await_count == 0


@pytest.mark.asyncio
async def test_structured_call_parse_recovers_when_recovery_enabled(
    metadata, db_mock, trace_mock, langfuse_url, no_langfuse, patch_anthropic_client, mocker
):
    partial = '{"item_reviews":[{"item_id":"a","reasoning":"hello'
    parse_exc = _make_validation_error(partial)
    patch_anthropic_client.messages.parse = mocker.AsyncMock(side_effect=parse_exc)
    call_api = mocker.patch(
        "rumil.llm.call_anthropic_api",
        new=mocker.AsyncMock(return_value=_api_response('"}]}')),
    )

    result = await _structured_call_parse(
        "system",
        _Schema,
        [{"role": "user", "content": "hi"}],
        metadata=metadata,
        db=db_mock,
        model="claude-opus-4-7",
        continuation_recovery=True,
    )

    assert isinstance(result.parsed, _Schema)
    assert result.response_text == partial + '"}]}'
    assert call_api.await_count == 1


@pytest.mark.asyncio
async def test_structured_call_parse_reraises_when_recovery_exhausts(
    metadata, db_mock, trace_mock, langfuse_url, no_langfuse, patch_anthropic_client, mocker
):
    partial = '{"item_reviews":[{"item_id":"a","reasoning":"hello'
    parse_exc = _make_validation_error(partial)
    patch_anthropic_client.messages.parse = mocker.AsyncMock(side_effect=parse_exc)
    mocker.patch(
        "rumil.llm.call_anthropic_api",
        new=mocker.AsyncMock(return_value=_api_response(" still bad")),
    )

    with pytest.raises(ValidationError):
        await _structured_call_parse(
            "system",
            _Schema,
            [{"role": "user", "content": "hi"}],
            metadata=metadata,
            db=db_mock,
            model="claude-opus-4-7",
            continuation_recovery=True,
        )
