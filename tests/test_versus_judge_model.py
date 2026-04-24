"""Regression tests for judge_model suffix parsing.

The three call sites (`judge.base_judge_model`, `judge.parse_judge_model_suffix`,
`analyze._strip_phash_version`) must agree on what a judge_model's "base" is —
otherwise the frontend's JudgeHeader and the backend's content-test baseline
lookup drift. This file pins the shared behavior.
"""

from __future__ import annotations

import pytest

from versus import analyze, judge

_BASES = (
    "openai/gpt-5.4",
    "rumil:ws:anthropic/claude-sonnet-4-6",
    "rumil:orch:anthropic/claude-sonnet-4-6:b4",
    "rumil:text:openai/gpt-5.4:general_quality",
    "anthropic:claude-sonnet-4-5",
)

_SUFFIX_VARIANTS = (
    "",
    ":p12345678",
    ":p12345678:v2",
    ":p12345678:v2:s87654321",
    ":p12345678:v2:t87654321",
    ":p12345678:v2:t87654321:s11223344",
    ":p12345678:v2:s11223344:t87654321",
)


@pytest.mark.parametrize("base", _BASES)
@pytest.mark.parametrize("suffix", _SUFFIX_VARIANTS)
def test_base_judge_model_strips_all_variants(base: str, suffix: str) -> None:
    assert judge.base_judge_model(base + suffix) == base


@pytest.mark.parametrize("base", _BASES)
@pytest.mark.parametrize("suffix", _SUFFIX_VARIANTS)
def test_base_judge_model_agrees_with_parse(base: str, suffix: str) -> None:
    jm = base + suffix
    assert judge.base_judge_model(jm) == judge.parse_judge_model_suffix(jm)[0]


@pytest.mark.parametrize("base", _BASES)
@pytest.mark.parametrize("suffix", _SUFFIX_VARIANTS)
def test_analyze_strip_agrees_with_judge_parse(base: str, suffix: str) -> None:
    jm = base + suffix
    parts, phash, version = analyze._strip_phash_version(jm.split(":"))
    jp_base, jp_phash, jp_version = judge.parse_judge_model_suffix(jm)
    assert ":".join(parts) == jp_base
    assert phash == jp_phash
    assert version == jp_version


def test_parse_returns_phash_and_version() -> None:
    base, phash, version = judge.parse_judge_model_suffix("openai/gpt-5.4:p12345678:v2:s87654321")
    assert base == "openai/gpt-5.4"
    assert phash == "p12345678"
    assert version == "v2"


def test_parse_legacy_unhashed_judge_model() -> None:
    base, phash, version = judge.parse_judge_model_suffix("openai/gpt-5.4")
    assert base == "openai/gpt-5.4"
    assert phash is None
    assert version is None


@pytest.mark.parametrize(
    ("judge_model", "expected_baseline"),
    (
        ("openai/gpt-5.4:p12345678:v2:s87654321", "paraphrase:openai/gpt-5.4"),
        ("google/gemini-3-flash-preview:p12345678", "paraphrase:google/gemini-3-flash-preview"),
        (
            "anthropic:claude-sonnet-4-5:p12345678:v2:s87654321",
            "paraphrase:anthropic/claude-sonnet-4-5",
        ),
        ("anthropic:claude-opus-4-7", "paraphrase:anthropic/claude-opus-4-7"),
    ),
)
def test_content_test_baseline_normalizes_anthropic(judge_model: str, expected_baseline: str) -> None:
    assert analyze._content_test_baseline(judge_model) == expected_baseline
