"""Tests for the Langfuse Cloud integration helpers.

Covers the disabled-by-default behaviour (the path most tests and CI run on)
plus structural guarantees on the LLMExchangeEvent that the frontend relies on
to render its "Langfuse ↗" link.
"""

import pytest

from rumil.settings import override_settings
from rumil.tracing import (
    flush_langfuse,
    get_langfuse,
    langfuse_trace_url_for_current_observation,
    observe,
    phase_span,
)
from rumil.tracing.langfuse_client import get_langfuse as _raw_get_langfuse
from rumil.tracing.trace_events import LLMExchangeEvent


@pytest.fixture(autouse=True)
def _clear_langfuse_singleton():
    """Reset the lru_cache between tests so settings overrides take effect."""
    _raw_get_langfuse.cache_clear()
    yield
    _raw_get_langfuse.cache_clear()


def test_get_langfuse_returns_none_when_disabled():
    with override_settings(langfuse_public_key="", langfuse_secret_key=""):
        assert get_langfuse() is None


def test_langfuse_enabled_property():
    with override_settings(langfuse_public_key="", langfuse_secret_key=""):
        from rumil.settings import get_settings

        assert get_settings().langfuse_enabled is False
    with override_settings(langfuse_public_key="pk-x", langfuse_secret_key="sk-y"):
        from rumil.settings import get_settings

        assert get_settings().langfuse_enabled is True


def test_trace_url_helper_returns_none_when_disabled():
    with override_settings(langfuse_public_key="", langfuse_secret_key=""):
        assert langfuse_trace_url_for_current_observation() is None


def test_phase_span_is_noop_when_disabled():
    with (
        override_settings(langfuse_public_key="", langfuse_secret_key=""),
        phase_span("anything"),
    ):
        assert True


def test_observe_decorator_passes_through_when_disabled():
    with override_settings(langfuse_public_key="", langfuse_secret_key=""):

        @observe()
        def double(x):
            return x * 2

        assert double(21) == 42


@pytest.mark.asyncio
async def test_observe_decorator_works_on_async_when_disabled():
    with override_settings(langfuse_public_key="", langfuse_secret_key=""):

        @observe()
        async def add_one(x):
            return x + 1

        assert await add_one(5) == 6


def test_flush_is_safe_when_disabled():
    with override_settings(langfuse_public_key="", langfuse_secret_key=""):
        flush_langfuse()


def test_llm_exchange_event_round_trips_without_langfuse_url():
    """Existing JSONB rows have no langfuse_trace_url — must deserialize cleanly."""
    payload = {
        "event": "llm_exchange",
        "exchange_id": "exch-123",
        "phase": "inner_loop",
        "round": 0,
        "input_tokens": 100,
        "output_tokens": 50,
        "duration_ms": 1234,
        "cost_usd": 0.001,
    }
    evt = LLMExchangeEvent.model_validate(payload)
    assert evt.langfuse_trace_url is None
    assert evt.exchange_id == "exch-123"
    re = LLMExchangeEvent.model_validate(evt.model_dump())
    assert re.langfuse_trace_url is None


def test_llm_exchange_event_carries_langfuse_url():
    url = "https://us.cloud.langfuse.com/project/abc/traces/xyz?observation=obs-1"
    evt = LLMExchangeEvent(
        exchange_id="exch-456",
        phase="closing_review",
        langfuse_trace_url=url,
    )
    dumped = evt.model_dump()
    assert dumped["langfuse_trace_url"] == url
    re = LLMExchangeEvent.model_validate(dumped)
    assert re.langfuse_trace_url == url
