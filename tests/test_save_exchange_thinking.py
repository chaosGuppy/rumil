"""`_save_exchange` forwards thinking_blocks to the DB and trace event.

Verifies the contract between `_save_exchange` and its two side
effects — the `db.save_llm_exchange` insert and the `LLMExchangeEvent`
record on the active CallTrace.
"""

import pytest

from rumil.llm import LLMExchangeMetadata, _save_exchange
from rumil.tracing.trace_events import LLMExchangeEvent


@pytest.fixture
def metadata() -> LLMExchangeMetadata:
    return LLMExchangeMetadata(
        call_id="call_123",
        phase="test_phase",
        round_num=0,
        user_message="hi",
    )


@pytest.fixture
def db_mock(mocker):
    db = mocker.MagicMock()
    db.save_llm_exchange = mocker.AsyncMock(return_value="exchange_abc")
    return db


@pytest.fixture
def trace_mock(mocker):
    """Patch get_trace() to return a mock trace whose record() is async."""
    trace = mocker.MagicMock()
    trace.record = mocker.AsyncMock()
    mocker.patch("rumil.llm.get_trace", return_value=trace)
    return trace


@pytest.fixture
def langfuse_url(mocker):
    mocker.patch("rumil.llm.langfuse_trace_url_for_current_observation", return_value=None)


def _recorded_event(trace_mock) -> LLMExchangeEvent:
    """Return the single LLMExchangeEvent recorded on the trace."""
    assert trace_mock.record.await_count == 1
    (event,), _ = trace_mock.record.await_args
    assert isinstance(event, LLMExchangeEvent)
    return event


@pytest.mark.asyncio
async def test_thinking_blocks_are_forwarded_to_db(metadata, db_mock, trace_mock, langfuse_url):
    payload = {"thinking": [{"content": "cot", "signature": "s1"}]}

    await _save_exchange(
        metadata,
        db=db_mock,
        model="claude-opus-4-7",
        system_prompt="sys",
        response_text="answer",
        tool_calls=[],
        input_tokens=10,
        output_tokens=20,
        duration_ms=100,
        thinking_blocks=payload,
    )

    db_mock.save_llm_exchange.assert_awaited_once()
    kwargs = db_mock.save_llm_exchange.await_args.kwargs
    assert kwargs["thinking_blocks"] == payload


@pytest.mark.asyncio
async def test_thinking_present_sets_has_thinking_true_on_event(
    metadata, db_mock, trace_mock, langfuse_url
):
    payload = {"redacted_thinking": [{"data": "opaque"}]}

    await _save_exchange(
        metadata,
        db=db_mock,
        model="claude-opus-4-7",
        system_prompt="sys",
        response_text="answer",
        tool_calls=[],
        input_tokens=1,
        output_tokens=1,
        duration_ms=1,
        thinking_blocks=payload,
    )

    event = _recorded_event(trace_mock)
    assert event.has_thinking is True
    assert event.exchange_id == "exchange_abc"


@pytest.mark.asyncio
async def test_no_thinking_passes_none_to_db_and_false_on_event(
    metadata, db_mock, trace_mock, langfuse_url
):
    await _save_exchange(
        metadata,
        db=db_mock,
        model="claude-haiku-4-5",
        system_prompt="sys",
        response_text="answer",
        tool_calls=[],
        input_tokens=1,
        output_tokens=1,
        duration_ms=1,
        # thinking_blocks defaults to None — no Haiku thinking
    )

    kwargs = db_mock.save_llm_exchange.await_args.kwargs
    assert kwargs["thinking_blocks"] is None
    assert _recorded_event(trace_mock).has_thinking is False
