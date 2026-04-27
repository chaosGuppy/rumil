"""Tests for versus.complete.extract_continuation.

The completion prompt invites models to think in scratch space before
writing the final answer, then wrap it in ``<continuation>...</continuation>``.
``extract_continuation`` pulls out the tagged content. Edge-case behaviour
(multiple tags, missing tag fallback, case insensitivity, multi-line
content) is worth pinning so future prompt tweaks don't silently change
what gets judged.
"""

from __future__ import annotations

import pytest

from versus import complete


def test_simple_tag_returns_inner_content() -> None:
    text = "<continuation>the essay continuation</continuation>"
    assert complete.extract_continuation(text) == "the essay continuation"


def test_preamble_before_tag_is_dropped() -> None:
    text = (
        "Let me plan first. I'll argue X by appealing to Y and Z.\n\n"
        "<continuation>The real continuation goes here.</continuation>"
    )
    assert complete.extract_continuation(text) == "The real continuation goes here."


def test_missing_tag_falls_back_to_full_text() -> None:
    text = "Model ignored the tag and just wrote a continuation directly."
    assert complete.extract_continuation(text) == text


def test_multiple_tags_keeps_last() -> None:
    text = (
        "<continuation>first draft</continuation>\n"
        "Actually let me revise.\n"
        "<continuation>better version</continuation>"
    )
    assert complete.extract_continuation(text) == "better version"


def test_inner_content_is_stripped() -> None:
    text = "<continuation>\n\n  padded continuation  \n\n</continuation>"
    assert complete.extract_continuation(text) == "padded continuation"


def test_fallback_is_also_stripped() -> None:
    assert complete.extract_continuation("   raw text  \n") == "raw text"


def test_multiline_content_preserved() -> None:
    text = "<continuation>## Heading\n\nFirst paragraph.\n\nSecond paragraph.</continuation>"
    expected = "## Heading\n\nFirst paragraph.\n\nSecond paragraph."
    assert complete.extract_continuation(text) == expected


@pytest.mark.parametrize(
    "opening,closing",
    (
        ("<CONTINUATION>", "</CONTINUATION>"),
        ("<Continuation>", "</Continuation>"),
    ),
)
def test_tag_matching_is_case_insensitive(opening: str, closing: str) -> None:
    text = f"preamble {opening}real text{closing}"
    assert complete.extract_continuation(text) == "real text"


def test_unclosed_tag_falls_back_to_full_text() -> None:
    text = "thinking... <continuation>started but never closed"
    assert complete.extract_continuation(text) == text


def test_empty_string_returns_empty() -> None:
    assert complete.extract_continuation("") == ""
