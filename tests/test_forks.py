"""Tests for exchange forks: side-effect-free re-runs of LLM exchanges."""

from typing import Any

import pytest

from rumil.forks import (
    BaseExchange,
    ForkOverrides,
    build_kwargs,
    fire_fork,
    hash_overrides,
    merge_overrides,
    resolve_base,
)
from rumil.models import CallType

# ---------- hash_overrides ----------


def test_hash_overrides_drops_nulls():
    h1 = hash_overrides({"system_prompt": "x"})
    h2 = hash_overrides({"system_prompt": "x", "model": None, "temperature": None})
    assert h1 == h2


def test_hash_overrides_is_stable_across_key_order():
    h1 = hash_overrides({"a": 1, "b": 2, "c": 3})
    h2 = hash_overrides({"c": 3, "a": 1, "b": 2})
    assert h1 == h2


def test_hash_overrides_changes_with_value():
    assert hash_overrides({"system_prompt": "alpha"}) != hash_overrides({"system_prompt": "beta"})


def test_hash_overrides_returns_short_hex():
    h = hash_overrides({"x": "y"})
    assert len(h) == 16
    assert all(c in "0123456789abcdef" for c in h)


# ---------- merge_overrides ----------


def _base(**overrides: Any) -> BaseExchange:
    fields: dict[str, Any] = {
        "exchange_id": "ex-1",
        "call_id": "call-1",
        "call_type": CallType.FIND_CONSIDERATIONS,
        "system_prompt": "base sys",
        "user_messages": [{"role": "user", "content": "hi"}],
        "tools": [{"name": "t1", "description": "tool one", "input_schema": {}}],
        "model": "claude-haiku-4-5-20251001",
        "temperature": 0.15,
        "max_tokens": 1000,
        "has_thinking": False,
        "thinking_off": False,
    }
    fields.update(overrides)
    return BaseExchange(**fields)


def test_merge_overrides_inherits_all_when_empty():
    base = _base()
    merged = merge_overrides(base, ForkOverrides())
    assert merged.system_prompt == base.system_prompt
    assert merged.user_messages == base.user_messages
    assert merged.tools == base.tools
    assert merged.model == base.model
    assert merged.temperature == base.temperature
    assert merged.max_tokens == base.max_tokens
    assert merged.thinking_off == base.thinking_off


def test_merge_overrides_replaces_set_fields():
    base = _base()
    merged = merge_overrides(
        base,
        ForkOverrides(system_prompt="new sys", temperature=0.7, max_tokens=42),
    )
    assert merged.system_prompt == "new sys"
    assert merged.temperature == 0.7
    assert merged.max_tokens == 42
    # Untouched
    assert merged.user_messages == base.user_messages
    assert merged.model == base.model


def test_merge_overrides_recomputes_has_thinking_for_overridden_model():
    """When the model changes, has_thinking must reflect the new model — not
    inherit from the base. Otherwise build_kwargs would attach `thinking` to
    a model that doesn't support it (or skip it on a model that does)."""
    base = _base(model="claude-haiku-4-5-20251001", has_thinking=False)
    merged = merge_overrides(base, ForkOverrides(model="claude-opus-4-7"))
    assert merged.model == "claude-opus-4-7"
    assert merged.has_thinking is True


def test_merge_overrides_thinking_off_propagates():
    base = _base(model="claude-opus-4-7", has_thinking=True, thinking_off=False)
    merged = merge_overrides(base, ForkOverrides(thinking_off=True))
    assert merged.thinking_off is True


# ---------- build_kwargs ----------


def test_build_kwargs_includes_required_api_fields():
    base = _base()
    k = build_kwargs(base)
    assert k["model"] == base.model
    assert k["max_tokens"] == base.max_tokens
    assert k["system"] == base.system_prompt
    assert k["messages"] == base.user_messages
    assert k["tools"] == base.tools


def test_build_kwargs_includes_thinking_for_adaptive_model():
    base = _base(model="claude-opus-4-7", has_thinking=True, thinking_off=False, temperature=None)
    k = build_kwargs(base)
    assert "thinking" in k


def test_build_kwargs_drops_thinking_when_off():
    base = _base(model="claude-opus-4-7", has_thinking=True, thinking_off=True, temperature=None)
    k = build_kwargs(base)
    assert "thinking" not in k


def test_build_kwargs_omits_thinking_for_models_without_it():
    base = _base(model="claude-haiku-4-5-20251001", has_thinking=False)
    k = build_kwargs(base)
    assert "thinking" not in k


def test_build_kwargs_includes_temperature_when_supported():
    base = _base(model="claude-haiku-4-5-20251001", temperature=0.5)
    k = build_kwargs(base)
    assert k["temperature"] == 0.5


def test_build_kwargs_omits_temperature_for_unsupported_model():
    """Opus 4.7 doesn't accept sampling params per the API."""
    base = _base(model="claude-opus-4-7", has_thinking=True, temperature=0.7)
    k = build_kwargs(base)
    assert "temperature" not in k


def test_build_kwargs_omits_tools_when_empty():
    base = _base(tools=[])
    k = build_kwargs(base)
    assert "tools" not in k


# ---------- DB helpers ----------

_FORK_DEFAULTS = dict(
    overrides={},
    model="claude-haiku-4-5-20251001",
    temperature=None,
    response_text=None,
    tool_calls=[],
    stop_reason=None,
    input_tokens=None,
    output_tokens=None,
    cache_creation_input_tokens=None,
    cache_read_input_tokens=None,
    duration_ms=None,
    cost_usd=None,
    error=None,
    created_by="test",
)


async def _seed_exchange(
    db, call_id: str, system_prompt: str = "sys", user_message: str = "hi"
) -> str:
    return await db.save_llm_exchange(
        call_id=call_id,
        phase="test",
        system_prompt=system_prompt,
        user_message=user_message,
        response_text="captured",
        tool_calls=[],
        input_tokens=10,
        output_tokens=5,
    )


async def test_save_fork_then_get_returns_persisted_row(tmp_db, scout_call):
    exchange_id = await _seed_exchange(tmp_db, scout_call.id)
    row = await tmp_db.save_fork(
        base_exchange_id=exchange_id,
        overrides_hash="abc1234567890abc",
        sample_index=0,
        **{
            **_FORK_DEFAULTS,
            "overrides": {"temperature": 0.7},
            "temperature": 0.7,
            "response_text": "forked",
        },
    )
    assert row.base_exchange_id == exchange_id
    fetched = await tmp_db.get_fork(row.id)
    assert fetched is not None
    assert fetched["response_text"] == "forked"
    assert fetched["overrides_hash"] == "abc1234567890abc"
    assert fetched["created_by"] == "test"


async def test_list_forks_for_exchange_returns_all(tmp_db, scout_call):
    exchange_id = await _seed_exchange(tmp_db, scout_call.id)
    for i in range(3):
        await tmp_db.save_fork(
            base_exchange_id=exchange_id,
            overrides_hash="hash1",
            sample_index=i,
            **_FORK_DEFAULTS,
        )
    rows = await tmp_db.list_forks_for_exchange(exchange_id)
    assert len(rows) == 3


async def test_list_forks_for_exchange_isolates_by_base(tmp_db, scout_call):
    e1 = await _seed_exchange(tmp_db, scout_call.id, user_message="msg one")
    e2 = await _seed_exchange(tmp_db, scout_call.id, user_message="msg two")
    await tmp_db.save_fork(
        base_exchange_id=e1, overrides_hash="h", sample_index=0, **_FORK_DEFAULTS
    )
    await tmp_db.save_fork(
        base_exchange_id=e2, overrides_hash="h", sample_index=0, **_FORK_DEFAULTS
    )
    rows1 = await tmp_db.list_forks_for_exchange(e1)
    rows2 = await tmp_db.list_forks_for_exchange(e2)
    assert len(rows1) == 1
    assert len(rows2) == 1
    assert rows1[0]["base_exchange_id"] == e1
    assert rows2[0]["base_exchange_id"] == e2


async def test_get_max_fork_sample_index_returns_max_per_hash(tmp_db, scout_call):
    exchange_id = await _seed_exchange(tmp_db, scout_call.id)
    assert await tmp_db.get_max_fork_sample_index(exchange_id, "h1") is None

    for i in range(3):
        await tmp_db.save_fork(
            base_exchange_id=exchange_id,
            overrides_hash="h1",
            sample_index=i,
            **_FORK_DEFAULTS,
        )
    assert await tmp_db.get_max_fork_sample_index(exchange_id, "h1") == 2
    # Different hash buckets are isolated
    assert await tmp_db.get_max_fork_sample_index(exchange_id, "other") is None


async def test_delete_fork_removes_row(tmp_db, scout_call):
    exchange_id = await _seed_exchange(tmp_db, scout_call.id)
    row = await tmp_db.save_fork(
        base_exchange_id=exchange_id, overrides_hash="h", sample_index=0, **_FORK_DEFAULTS
    )
    assert await tmp_db.get_fork(row.id) is not None
    await tmp_db.delete_fork(row.id)
    assert await tmp_db.get_fork(row.id) is None


# ---------- resolve_base ----------


async def test_resolve_base_reconstructs_inputs(tmp_db, scout_call):
    exchange_id = await _seed_exchange(
        tmp_db, scout_call.id, system_prompt="real sys", user_message="real msg"
    )
    base = await resolve_base(tmp_db, exchange_id)
    assert base.exchange_id == exchange_id
    assert base.call_id == scout_call.id
    assert base.call_type == CallType.FIND_CONSIDERATIONS
    assert base.system_prompt == "real sys"
    # When user_messages column isn't populated, the legacy single user_message
    # is wrapped into a one-message list.
    assert len(base.user_messages) == 1
    assert base.user_messages[0]["content"] == "real msg"
    # FIND_CONSIDERATIONS preset has tools
    assert len(base.tools) > 0


async def test_resolve_base_raises_on_missing_id(tmp_db):
    with pytest.raises(ValueError):
        await resolve_base(tmp_db, "00000000-0000-0000-0000-000000000000")


# ---------- fire_fork (real LLM, end-to-end) ----------


@pytest.mark.llm
async def test_fire_fork_persists_row_with_response(tmp_db, scout_call):
    """End-to-end: fire 1 sample with no overrides. The base is replayed
    against Haiku (test-mode default model) and a row lands in the table."""
    exchange_id = await _seed_exchange(
        tmp_db,
        scout_call.id,
        system_prompt="You are a terse helper. Reply with one short word and stop.",
        user_message="Reply 'ok'.",
    )
    rows = await fire_fork(
        tmp_db, exchange_id, ForkOverrides(), n_samples=1, created_by="test-fire"
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.base_exchange_id == exchange_id
    assert row.error is None
    assert row.input_tokens is not None and row.input_tokens > 0
    assert row.output_tokens is not None
    assert row.created_by == "test-fire"
    # Roundtrips through list_forks_for_exchange
    listed = await tmp_db.list_forks_for_exchange(exchange_id)
    assert any(r["id"] == row.id for r in listed)
