"""Tests for versus's tiny jsonl store.

Focus: the cache + truncated-line tolerance. Dedup behavior is exercised
transitively by mirror-key / blind-judge tests; this module pins the
crash-on-truncated-write case that used to bubble a JSONDecodeError up
through the API into a 500.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

_VERSUS_SRC = Path(__file__).resolve().parents[1] / "versus" / "src"
if str(_VERSUS_SRC) not in sys.path:
    sys.path.insert(0, str(_VERSUS_SRC))

from versus import jsonl as versus_jsonl  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_cache():
    versus_jsonl._READ_CACHE.clear()
    yield
    versus_jsonl._READ_CACHE.clear()


def test_read_tolerates_truncated_final_line(tmp_path, caplog):
    path = tmp_path / "log.jsonl"
    path.write_text(
        '{"key": "a", "v": 1}\n'
        '{"key": "b", "v": 2}\n'
        '{"key": "c", "v": 3',  # writer crashed mid-append: no closing brace, no newline
    )
    with caplog.at_level(logging.WARNING, logger="versus.jsonl"):
        rows = list(versus_jsonl.read(path))
    assert [r["key"] for r in rows] == ["a", "b"]
    assert any("truncated final line" in rec.message for rec in caplog.records)


def test_read_tolerates_truncated_final_line_with_trailing_newline(tmp_path):
    path = tmp_path / "log.jsonl"
    path.write_text('{"key": "a"}\n{"key": "b"\n')
    rows = list(versus_jsonl.read(path))
    assert [r["key"] for r in rows] == ["a"]


def test_read_raises_on_mid_file_corruption(tmp_path):
    path = tmp_path / "log.jsonl"
    path.write_text('{"key": "a"}\nGARBAGE\n{"key": "b"}\n')
    with pytest.raises(json.JSONDecodeError):
        list(versus_jsonl.read(path))


def test_read_missing_file_returns_empty(tmp_path):
    assert list(versus_jsonl.read(tmp_path / "does_not_exist.jsonl")) == []


def test_append_invalidates_cache(tmp_path):
    path = tmp_path / "log.jsonl"
    path.write_text('{"key": "a"}\n')
    assert [r["key"] for r in versus_jsonl.read(path)] == ["a"]
    versus_jsonl.append(path, {"key": "b"})
    assert [r["key"] for r in versus_jsonl.read(path)] == ["a", "b"]
