"""State file tracking which calls have been LLM-scanned for confusion.

Persists to .claude/state/rumil-scan-log.json so repeat invocations of
find_confusion don't re-scan the same traces (each deep scan costs real
money). The log is per-worktree so different branches can have different
scan coverage.

Schema:

    {
      "calls": {
        "<call_id>": {
          "scanned_at": "<iso timestamp>",
          "model": "<claude model id>",
          "verdict": "confused" | "ok" | "inconclusive",
          "severity": 1-5,
          "primary_symptom": "<short label>",
          "evidence": ["<quote 1>", "<quote 2>"],
          "suggested_action": "<label>"
        }
      }
    }
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCAN_LOG_PATH = Path(".claude/state/rumil-scan-log.json")


def load_scan_log() -> dict[str, Any]:
    if not SCAN_LOG_PATH.exists():
        return {"calls": {}}
    try:
        data = json.loads(SCAN_LOG_PATH.read_text())
    except json.JSONDecodeError:
        return {"calls": {}}
    if "calls" not in data:
        data["calls"] = {}
    return data


def save_scan_log(log: dict[str, Any]) -> None:
    SCAN_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    SCAN_LOG_PATH.write_text(json.dumps(log, indent=2))


def is_scanned(log: dict[str, Any], call_id: str) -> bool:
    return call_id in log.get("calls", {})


def get_scan(log: dict[str, Any], call_id: str) -> dict[str, Any] | None:
    return log.get("calls", {}).get(call_id)


def filter_unscanned(log: dict[str, Any], call_ids: list[str]) -> list[str]:
    scanned = log.get("calls", {})
    return [c for c in call_ids if c not in scanned]


def record_scan(
    log: dict[str, Any],
    call_id: str,
    *,
    model: str,
    verdict: str,
    severity: int | None,
    primary_symptom: str,
    evidence: list[str],
    suggested_action: str,
) -> None:
    """Mutate ``log`` in place to record a scan result. Caller saves."""
    log.setdefault("calls", {})[call_id] = {
        "scanned_at": datetime.now(UTC).isoformat(),
        "model": model,
        "verdict": verdict,
        "severity": severity,
        "primary_symptom": primary_symptom,
        "evidence": evidence,
        "suggested_action": suggested_action,
    }
