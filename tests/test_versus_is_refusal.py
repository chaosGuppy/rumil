"""Regression tests for versus.judge.is_refusal.

is_refusal is the gatekeeper on pair enumeration -- a row that slips
through becomes a contestant the judge sees. Concrete bugs we're pinning
against: empty model responses (200 OK with no text) silently being
judged, and short human baselines being incorrectly excluded.

Operates on DB-shaped versus_texts rows: ``kind``, ``response``, ``text``.
"""

from __future__ import annotations

import sys
from pathlib import Path

_VERSUS_SRC = Path(__file__).resolve().parents[1] / "versus" / "src"
if str(_VERSUS_SRC) not in sys.path:
    sys.path.insert(0, str(_VERSUS_SRC))

from versus.judge import is_refusal  # noqa: E402


def _completion_row(**overrides):
    row = {
        "kind": "completion",
        "text": "A rich, substantive continuation that goes on for many words indeed.",
        "response": {"choices": [{"finish_reason": "stop"}]},
    }
    row.update(overrides)
    return row


def test_substantive_completion_is_not_refusal():
    assert is_refusal(_completion_row()) is False


def test_content_filter_is_refusal():
    assert (
        is_refusal(_completion_row(response={"choices": [{"finish_reason": "content_filter"}]}))
        is True
    )


def test_native_refusal_reason_is_refusal():
    assert (
        is_refusal(_completion_row(response={"choices": [{"native_finish_reason": "refusal"}]}))
        is True
    )


def test_empty_completion_is_refusal():
    assert is_refusal(_completion_row(text="")) is True


def test_tiny_completion_is_refusal():
    # A two-word "response" is not a judgable continuation.
    assert is_refusal(_completion_row(text="Sure, okay.")) is True


def test_human_row_is_never_refusal():
    # Human baseline rows have response=None and kind=human. The held-out
    # remainder is canonically truth; don't exclude it even if it's short.
    row = {
        "kind": "human",
        "text": "short",
        "response": None,
    }
    assert is_refusal(row) is False
