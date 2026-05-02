"""Pure-function tests for the DB-row → legacy-JSONL translators in versus_router.

Any drift in these functions silently breaks the frontend (it consumes
the legacy field names), so they need pinned coverage even though they're
boring shape-conversions.
"""

from __future__ import annotations

import pytest

from rumil.api.versus_router import (
    _legacy_judgment_dict,
    _legacy_text_dict,
    _other_source,
    _trace_url,
    _user_prompt_from_request,
)

EXPECTED_JUDGMENT_KEYS = {
    "key",
    "essay_id",
    "prefix_config_hash",
    "source_a",
    "source_b",
    "display_first",
    "display_second",
    "criterion",
    "judge_model",
    "verdict",
    "winner_source",
    "preference_label",
    "reasoning_text",
    "prompt",
    "system_prompt",
    "raw_response",
    "ts",
    "duration_s",
    "config",
    "config_hash",
    "sampling",
    "rumil_call_id",
    "rumil_run_id",
    "rumil_question_id",
    "rumil_trace_url",
    "rumil_cost_usd",
    "contamination_note",
}


def _judgment_row(**overrides):
    row = {
        "id": "00000000-0000-0000-0000-000000000001",
        "essay_id": "redwood__some-essay",
        "prefix_hash": "abcd1234",
        "source_a": "google/gemini-3-flash-preview",
        "source_b": "human",
        "display_first": "human",
        "criterion": "general_quality",
        "judge_model": "blind:google/gemini-3-flash-preview:general_quality:c1234567",
        "verdict": "B",
        "winner_source": "google/gemini-3-flash-preview",
        "preference_label": "B somewhat preferred",
        "reasoning_text": "Continuation B is...",
        "request": None,
        "response": None,
        "judge_inputs": {
            "model": "google/gemini-3-flash-preview",
            "model_config": {
                "temperature": 0.0,
                "max_tokens": 2048,
                "top_p": None,
                "thinking": None,
                "effort": None,
                "max_thinking_tokens": None,
                "service_tier": None,
            },
            "variant": "blind",
        },
        "judge_inputs_hash": "deadbeef",
        "duration_s": 4.2,
        "created_at": "2026-04-29T00:00:00+00:00",
        "rumil_call_id": None,
        "run_id": None,
        "rumil_question_id": None,
        "rumil_cost_usd": None,
        "contamination_note": None,
    }
    row.update(overrides)
    return row


def test_legacy_judgment_dict_emits_full_key_set():
    legacy = _legacy_judgment_dict(_judgment_row())
    assert set(legacy.keys()) == EXPECTED_JUDGMENT_KEYS


def test_legacy_judgment_dict_renames_prefix_hash_to_prefix_config_hash():
    legacy = _legacy_judgment_dict(_judgment_row())
    assert legacy["prefix_config_hash"] == "abcd1234"


def test_legacy_judgment_dict_passes_through_winner_source():
    legacy = _legacy_judgment_dict(_judgment_row(winner_source="human"))
    assert legacy["winner_source"] == "human"


def test_legacy_judgment_dict_anthropic_request_extracts_system_and_user():
    request = {
        "model": "claude-opus-4-7",
        "system": "you are a blind judge",
        "messages": [{"role": "user", "content": "compare A and B"}],
    }
    legacy = _legacy_judgment_dict(_judgment_row(request=request))
    assert legacy["system_prompt"] == "you are a blind judge"
    assert legacy["prompt"] == "compare A and B"


def test_legacy_judgment_dict_openrouter_request_extracts_system_and_user():
    request = {
        "model": "google/gemini-3-flash-preview",
        "messages": [
            {"role": "system", "content": "you are a blind judge"},
            {"role": "user", "content": "compare A and B"},
        ],
    }
    legacy = _legacy_judgment_dict(_judgment_row(request=request))
    assert legacy["system_prompt"] == "you are a blind judge"
    assert legacy["prompt"] == "compare A and B"


def test_legacy_judgment_dict_null_request_yields_none_prompts():
    legacy = _legacy_judgment_dict(_judgment_row(request=None))
    assert legacy["prompt"] is None
    assert legacy["system_prompt"] is None


def test_legacy_judgment_dict_response_aliased_to_raw_response():
    response = {"id": "msg_x", "choices": [{"finish_reason": "stop"}]}
    legacy = _legacy_judgment_dict(_judgment_row(response=response))
    assert legacy["raw_response"] == response


def test_legacy_judgment_dict_run_id_aliased_to_rumil_run_id():
    legacy = _legacy_judgment_dict(_judgment_row(run_id="run-abc"))
    assert legacy["rumil_run_id"] == "run-abc"


def test_legacy_judgment_dict_judge_inputs_become_config_and_sampling():
    legacy = _legacy_judgment_dict(_judgment_row())
    assert legacy["config"]["model"] == "google/gemini-3-flash-preview"
    assert legacy["sampling"] == {"temperature": 0.0, "max_tokens": 2048}
    assert legacy["config_hash"] == "deadbeef"


def test_legacy_judgment_dict_id_becomes_key():
    legacy = _legacy_judgment_dict(_judgment_row())
    assert legacy["key"] == "00000000-0000-0000-0000-000000000001"


@pytest.mark.parametrize(
    ("display_first", "expected_second"),
    [
        ("google/gemini-3-flash-preview", "human"),
        ("human", "google/gemini-3-flash-preview"),
    ],
)
def test_other_source_resolves_display_second(display_first, expected_second):
    row = {
        "source_a": "google/gemini-3-flash-preview",
        "source_b": "human",
        "display_first": display_first,
    }
    assert _other_source(row) == expected_second


def test_other_source_returns_none_for_mismatch():
    row = {"source_a": "x", "source_b": "y", "display_first": "z"}
    assert _other_source(row) is None


def test_trace_url_none_without_run():
    assert _trace_url(None, None) is None
    assert _trace_url(None, "call-anything") is None


def test_trace_url_run_only_no_anchor():
    url = _trace_url("run-abc", None)
    assert url is not None
    assert url.endswith("/traces/run-abc")
    assert "#call" not in url


def test_trace_url_includes_call_anchor():
    url = _trace_url("run-abc", "call-12345678abcdef")
    assert url is not None
    assert url.endswith("/traces/run-abc#call-call-123")


@pytest.mark.parametrize(
    ("messages", "expected"),
    [
        ([{"role": "user", "content": "hi"}], "hi"),
        (
            [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "user msg"},
            ],
            "user msg",
        ),
        ([{"role": "system", "content": "only-sys"}], "only-sys"),
        ([], None),
    ],
)
def test_user_prompt_from_request_resolves_user_or_first(messages, expected):
    request = {"messages": messages} if messages or messages == [] else None
    assert _user_prompt_from_request(request) == expected


def test_user_prompt_from_request_returns_none_for_non_dict():
    assert _user_prompt_from_request(None) is None
    assert _user_prompt_from_request("not a dict") is None  # pyright: ignore[reportArgumentType]


def _text_row(**overrides):
    row = {
        "id": "11111111-1111-1111-1111-111111111111",
        "essay_id": "redwood__some-essay",
        "kind": "completion",
        "source_id": "google/gemini-3-flash-preview",
        "prefix_hash": "pfx9876",
        "model_id": "google/gemini-3-flash-preview",
        "params": {"temperature": 0.7, "target_words": 1000, "provider": "openrouter"},
        "request": {
            "model": "google/gemini-3-flash-preview",
            "messages": [{"role": "user", "content": "continue this..."}],
        },
        "response": {"id": "gen-abc"},
        "text": "this is the continuation that the model produced",
        "created_at": "2026-04-29T00:00:00+00:00",
    }
    row.update(overrides)
    return row


def test_legacy_text_dict_human_row_has_no_request_or_response():
    row = _text_row(
        kind="human",
        source_id="human",
        model_id=None,
        request=None,
        response=None,
        text="held-out remainder",
    )
    legacy = _legacy_text_dict(row)
    assert legacy["source_kind"] == "human"
    assert legacy["raw_response"] is None
    assert legacy["prompt"] is None
    assert legacy["response_text"] == "held-out remainder"


def test_legacy_text_dict_completion_row_carries_prompt_and_response():
    row = _text_row()
    legacy = _legacy_text_dict(row)
    assert legacy["source_kind"] == "completion"
    assert legacy["prompt"] == "continue this..."
    assert legacy["raw_response"] == {"id": "gen-abc"}
    assert legacy["response_text"].startswith("this is the continuation")


def test_legacy_text_dict_renames_prefix_hash_and_kind():
    legacy = _legacy_text_dict(_text_row())
    assert legacy["prefix_config_hash"] == "pfx9876"
    assert legacy["source_kind"] == "completion"


def test_legacy_text_dict_target_words_pulled_from_params():
    legacy = _legacy_text_dict(_text_row())
    assert legacy["target_words"] == 1000


def test_legacy_text_dict_response_words_uses_column_when_light_projection():
    row = _text_row(response_words=42)
    legacy = _legacy_text_dict(row)
    assert legacy["response_words"] == 42


def test_legacy_text_dict_response_words_computed_when_heavy_projection():
    row = _text_row(text="one two   three\tfour")
    assert "response_words" not in row
    legacy = _legacy_text_dict(row)
    assert legacy["response_words"] == 4


def test_legacy_text_dict_params_drops_synthetic_keys():
    row = _text_row(params={"temperature": 0.7, "target_words": 1000, "provider": "openrouter"})
    legacy = _legacy_text_dict(row)
    assert "target_words" not in legacy["params"]
    assert "provider" not in legacy["params"]
    assert legacy["params"]["temperature"] == 0.7
