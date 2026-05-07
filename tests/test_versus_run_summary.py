"""Tests for versus.run_summary.RunSummary.

The accumulator pulls token totals from either Anthropic-shaped
(input_tokens / output_tokens) or OpenRouter-shaped (prompt_tokens /
completion_tokens) usage dicts, and accepts an optional cost_usd for
rumil-mediated paths where we have a real $ figure.
"""

import pytest
from versus.run_summary import RunSummary


def test_initial_state_is_zero() -> None:
    s = RunSummary()
    assert s.n_done == 0
    assert s.n_err == 0
    assert s.in_tokens == 0
    assert s.out_tokens == 0
    assert s.cost_usd == 0.0


def test_record_success_with_anthropic_usage() -> None:
    s = RunSummary()
    s.record_success({"usage": {"input_tokens": 1200, "output_tokens": 340}})
    assert s.n_done == 1
    assert s.in_tokens == 1200
    assert s.out_tokens == 340


def test_record_success_with_openrouter_usage() -> None:
    s = RunSummary()
    s.record_success({"usage": {"prompt_tokens": 800, "completion_tokens": 200}})
    assert s.n_done == 1
    assert s.in_tokens == 800
    assert s.out_tokens == 200


def test_record_success_accumulates_across_calls() -> None:
    s = RunSummary()
    s.record_success({"usage": {"input_tokens": 100, "output_tokens": 50}})
    s.record_success({"usage": {"prompt_tokens": 200, "completion_tokens": 75}})
    assert s.n_done == 2
    assert s.in_tokens == 300
    assert s.out_tokens == 125


def test_record_success_with_none_response_increments_count_only() -> None:
    s = RunSummary()
    s.record_success(None)
    assert s.n_done == 1
    assert s.in_tokens == 0
    assert s.out_tokens == 0


def test_record_success_with_no_usage_field_is_safe() -> None:
    s = RunSummary()
    s.record_success({"id": "msg_123", "content": []})
    assert s.n_done == 1
    assert s.in_tokens == 0
    assert s.out_tokens == 0


@pytest.mark.parametrize("garbage", ("not-a-dict", 42, [1, 2, 3], object()))
def test_record_success_with_non_dict_response_is_safe(garbage) -> None:
    s = RunSummary()
    s.record_success(garbage)
    assert s.n_done == 1
    assert s.in_tokens == 0


def test_record_success_with_non_dict_usage_is_safe() -> None:
    s = RunSummary()
    s.record_success({"usage": "broken"})
    assert s.n_done == 1
    assert s.in_tokens == 0


def test_record_success_with_cost_usd_accumulates() -> None:
    s = RunSummary()
    s.record_success(cost_usd=0.0123)
    s.record_success(cost_usd=0.4567)
    assert s.n_done == 2
    assert s.cost_usd == pytest.approx(0.469)


def test_record_error_increments_only_error_count() -> None:
    s = RunSummary()
    s.record_error()
    s.record_error()
    assert s.n_err == 2
    assert s.n_done == 0
    assert s.in_tokens == 0


def test_print_emits_summary_line_with_label_and_counts(capsys) -> None:
    s = RunSummary()
    s.record_success({"usage": {"input_tokens": 1000, "output_tokens": 500}})
    s.record_error()
    s.print("completions")

    out = capsys.readouterr().out
    assert "[summary]" in out
    assert "completions" in out
    assert "1 done" in out
    assert "1 errors" in out
    assert "1,000" in out
    assert "500" in out
    assert "wall" in out


def test_print_omits_tokens_section_when_no_tokens_recorded(capsys) -> None:
    s = RunSummary()
    s.record_success(None)
    s.print("orch judgments")

    out = capsys.readouterr().out
    assert "tokens" not in out
    assert "1 done" in out


def test_print_includes_cost_when_nonzero(capsys) -> None:
    s = RunSummary()
    s.record_success(cost_usd=1.2345)
    s.print("ws judgments")

    out = capsys.readouterr().out
    assert "$1.2345" in out


def test_print_omits_cost_when_zero(capsys) -> None:
    s = RunSummary()
    s.record_success({"usage": {"input_tokens": 10, "output_tokens": 5}})
    s.print("completions")

    out = capsys.readouterr().out
    assert "$" not in out
