"""Tests for rumil_skills.scan_log JSONL scan-log reader/writer."""

from __future__ import annotations

import json

import pytest

from rumil_skills import scan_log


@pytest.fixture(autouse=True)
def _chdir_tmp(tmp_path, monkeypatch):
    """Redirect SCAN_LOG_PATH into tmp_path so tests never touch real .claude/."""
    monkeypatch.chdir(tmp_path)


def test_load_returns_empty_when_file_missing():
    log = scan_log.load_scan_log()
    assert log == {"calls": {}}


def test_load_returns_empty_on_corrupt_json(tmp_path):
    scan_log.SCAN_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    scan_log.SCAN_LOG_PATH.write_text("{not valid json")
    log = scan_log.load_scan_log()
    assert log == {"calls": {}}


def test_load_fills_calls_key_when_missing():
    scan_log.SCAN_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    scan_log.SCAN_LOG_PATH.write_text(json.dumps({"something_else": 1}))
    log = scan_log.load_scan_log()
    assert "calls" in log
    assert log["calls"] == {}
    assert log["something_else"] == 1


def test_save_creates_parent_dir_and_round_trips():
    log = {"calls": {"abc": {"verdict": "ok"}}}
    scan_log.save_scan_log(log)
    assert scan_log.SCAN_LOG_PATH.exists()
    reloaded = scan_log.load_scan_log()
    assert reloaded == log


def test_is_scanned_true_after_record():
    log = scan_log.load_scan_log()
    scan_log.record_scan(
        log,
        "call-1",
        model="claude-haiku",
        verdict="ok",
        severity=1,
        primary_symptom="none",
        evidence=[],
        suggested_action="ignore",
    )
    assert scan_log.is_scanned(log, "call-1")
    assert not scan_log.is_scanned(log, "call-2")


def test_is_scanned_on_empty_log():
    assert not scan_log.is_scanned({"calls": {}}, "anything")
    assert not scan_log.is_scanned({}, "anything")


def test_get_scan_returns_record():
    log = {"calls": {}}
    scan_log.record_scan(
        log,
        "call-1",
        model="claude-haiku",
        verdict="confused",
        severity=4,
        primary_symptom="loop",
        evidence=["quote a", "quote b"],
        suggested_action="retry",
    )
    entry = scan_log.get_scan(log, "call-1")
    assert entry is not None
    assert entry["verdict"] == "confused"
    assert entry["severity"] == 4
    assert entry["primary_symptom"] == "loop"
    assert entry["evidence"] == ["quote a", "quote b"]
    assert entry["suggested_action"] == "retry"
    assert entry["model"] == "claude-haiku"
    assert "scanned_at" in entry


def test_get_scan_missing_returns_none():
    assert scan_log.get_scan({"calls": {}}, "nope") is None
    assert scan_log.get_scan({}, "nope") is None


def test_filter_unscanned_excludes_already_scanned():
    log = {"calls": {}}
    scan_log.record_scan(
        log,
        "call-1",
        model="m",
        verdict="ok",
        severity=None,
        primary_symptom="",
        evidence=[],
        suggested_action="",
    )
    unscanned = scan_log.filter_unscanned(log, ["call-1", "call-2", "call-3"])
    assert unscanned == ["call-2", "call-3"]


def test_filter_unscanned_preserves_order():
    log = {"calls": {"b": {}}}
    unscanned = scan_log.filter_unscanned(log, ["a", "b", "c", "d"])
    assert unscanned == ["a", "c", "d"]


def test_filter_unscanned_on_empty_log():
    assert scan_log.filter_unscanned({"calls": {}}, ["x", "y"]) == ["x", "y"]


def test_record_scan_overwrites_existing_entry():
    log = {"calls": {}}
    scan_log.record_scan(
        log,
        "call-1",
        model="m1",
        verdict="ok",
        severity=1,
        primary_symptom="first",
        evidence=[],
        suggested_action="",
    )
    scan_log.record_scan(
        log,
        "call-1",
        model="m2",
        verdict="confused",
        severity=5,
        primary_symptom="second",
        evidence=["e"],
        suggested_action="fix",
    )
    entry = scan_log.get_scan(log, "call-1")
    assert entry is not None
    assert entry["verdict"] == "confused"
    assert entry["primary_symptom"] == "second"
    assert entry["model"] == "m2"


def test_record_scan_creates_calls_key_if_missing():
    log: dict = {}
    scan_log.record_scan(
        log,
        "call-1",
        model="m",
        verdict="ok",
        severity=None,
        primary_symptom="",
        evidence=[],
        suggested_action="",
    )
    assert "call-1" in log["calls"]


def test_save_then_load_preserves_all_fields():
    log = {"calls": {}}
    scan_log.record_scan(
        log,
        "call-1",
        model="claude-opus",
        verdict="inconclusive",
        severity=3,
        primary_symptom="unclear",
        evidence=["q1"],
        suggested_action="rescan",
    )
    scan_log.save_scan_log(log)
    reloaded = scan_log.load_scan_log()
    entry = reloaded["calls"]["call-1"]
    assert entry["model"] == "claude-opus"
    assert entry["verdict"] == "inconclusive"
    assert entry["severity"] == 3
    assert entry["evidence"] == ["q1"]
