"""Regression tests for versus.judge.is_refusal and its sibling paraphrase check.

These are gatekeepers on pair enumeration -- a row that slips through
is_refusal becomes a contestant the judge sees. Concrete bugs we're
pinning against: empty model responses (200 OK with no text) silently
being judged, and paraphrase-remainder rows inheriting the refusal
state of a refused upstream paraphrase.
"""

from __future__ import annotations

import sys
from pathlib import Path

_VERSUS_SRC = Path(__file__).resolve().parents[1] / "versus" / "src"
if str(_VERSUS_SRC) not in sys.path:
    sys.path.insert(0, str(_VERSUS_SRC))

from versus.complete import _paraphrase_was_refused  # noqa: E402
from versus.judge import is_refusal  # noqa: E402


def _completion_row(**overrides):
    row = {
        "source_kind": "completion",
        "response_text": "A rich, substantive continuation that goes on for many words indeed.",
        "raw_response": {"choices": [{"finish_reason": "stop"}]},
    }
    row.update(overrides)
    return row


def test_substantive_completion_is_not_refusal():
    assert is_refusal(_completion_row()) is False


def test_content_filter_is_refusal():
    assert (
        is_refusal(_completion_row(raw_response={"choices": [{"finish_reason": "content_filter"}]}))
        is True
    )


def test_native_refusal_reason_is_refusal():
    assert (
        is_refusal(_completion_row(raw_response={"choices": [{"native_finish_reason": "refusal"}]}))
        is True
    )


def test_empty_completion_is_refusal():
    assert is_refusal(_completion_row(response_text="")) is True


def test_tiny_completion_is_refusal():
    # A two-word "response" is not a judgable continuation.
    assert is_refusal(_completion_row(response_text="Sure, okay.")) is True


def test_human_row_is_never_refusal():
    # Human baseline rows have raw_response=None and source_kind=human.
    # The held-out remainder is canonically truth; don't exclude it even
    # if it's short.
    row = {
        "source_kind": "human",
        "response_text": "short",
        "raw_response": None,
    }
    assert is_refusal(row) is False


def test_paraphrase_remainder_with_refusal_flag_is_refusal():
    # ensure_paraphrase_rows stamps paraphrase_refusal=True on the derived
    # row when the upstream paraphrase was refused; is_refusal must honor it.
    row = {
        "source_kind": "paraphrase",
        "response_text": "derived text",
        "raw_response": None,
        "paraphrase_refusal": True,
    }
    assert is_refusal(row) is True


def test_paraphrase_remainder_without_flag_is_not_refusal():
    row = {
        "source_kind": "paraphrase",
        "response_text": "derived text",
        "raw_response": None,
    }
    assert is_refusal(row) is False


def test_paraphrase_was_refused_content_filter():
    para = {"raw_response": {"choices": [{"finish_reason": "content_filter"}]}, "response_text": ""}
    assert _paraphrase_was_refused(para) is True


def test_paraphrase_was_refused_empty_response():
    para = {"raw_response": {"choices": [{"finish_reason": "stop"}]}, "response_text": ""}
    assert _paraphrase_was_refused(para) is True


def test_paraphrase_was_refused_normal_response():
    text = " ".join(["word"] * 200)
    para = {"raw_response": {"choices": [{"finish_reason": "stop"}]}, "response_text": text}
    assert _paraphrase_was_refused(para) is False
