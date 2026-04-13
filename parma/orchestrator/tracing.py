"""Trace event recording for orchestrator and chat runs.

Records ModelEvents, ToolEvents, and span markers to the trace_events table.
All events carry a span_id for hierarchy reconstruction and a run_id for
grouping. The frontend operator UI reads these back via the operator API.
"""

import json
import sqlite3
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

PRICING: dict[str, dict[str, float]] = {
    "claude-sonnet-4-6": {
        "input": 3.0, "output": 15.0,
        "cache_read": 0.30, "cache_write": 3.75,
    },
    "claude-opus-4-6": {
        "input": 15.0, "output": 75.0,
        "cache_read": 1.50, "cache_write": 18.75,
    },
    "claude-haiku-4-5-20251001": {
        "input": 0.80, "output": 4.0,
        "cache_read": 0.08, "cache_write": 1.0,
    },
}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex[:16]


def compute_cost(model: str, usage: dict[str, int]) -> float:
    prices = PRICING.get(model, PRICING["claude-sonnet-4-6"])
    return (
        usage.get("input_tokens", 0) * prices["input"]
        + usage.get("output_tokens", 0) * prices["output"]
        + usage.get("cache_read_input_tokens", 0) * prices["cache_read"]
        + usage.get("cache_creation_input_tokens", 0) * prices["cache_write"]
    ) / 1_000_000


@dataclass
class RunTracer:
    """Records trace events for a single run."""

    conn: sqlite3.Connection
    run_id: str
    _events: list[dict[str, Any]] = field(default_factory=list)
    _total_cost: float = 0.0
    _total_input: int = 0
    _total_output: int = 0
    _total_cache_read: int = 0
    _total_cache_write: int = 0
    _model_calls: int = 0
    _tool_calls: int = 0

    def span_begin(
        self,
        span_id: str,
        span_type: str,
        name: str,
        parent_span_id: str | None = None,
    ) -> str:
        event_id = _new_id()
        self._write_event(event_id, "span_begin", span_id, parent_span_id, {
            "span_type": span_type,
            "name": name,
        })
        return span_id

    def span_end(self, span_id: str) -> None:
        self._write_event(_new_id(), "span_end", span_id, None, {})

    def record_model_event(
        self,
        span_id: str,
        *,
        model: str,
        temperature: float,
        max_tokens: int,
        input_messages: Sequence[dict[str, Any]],
        tools_offered: Sequence[dict[str, Any]],
        output_content: Sequence[dict[str, Any]],
        stop_reason: str,
        usage: dict[str, int],
        duration_ms: int,
    ) -> None:
        cost = compute_cost(model, usage)
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        cache_read = usage.get("cache_read_input_tokens", 0)
        cache_write = usage.get("cache_creation_input_tokens", 0)

        self._total_cost += cost
        self._total_input += input_tokens
        self._total_output += output_tokens
        self._total_cache_read += cache_read
        self._total_cache_write += cache_write
        self._model_calls += 1

        self._write_event(_new_id(), "model", span_id, None, {
            "config": {
                "model": model,
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
            "input_messages": list(input_messages),
            "tools_offered": [{"name": t.get("name", ""), "description": t.get("description", "")} for t in tools_offered],
            "output_content": list(output_content),
            "stop_reason": stop_reason,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_tokens": cache_read,
                "cache_write_tokens": cache_write,
            },
            "cost_usd": round(cost, 6),
            "duration_ms": duration_ms,
        })

    def record_tool_event(
        self,
        span_id: str,
        *,
        function_name: str,
        arguments: dict[str, Any],
        result: str,
        error: str | None = None,
        duration_ms: int = 0,
    ) -> None:
        self._tool_calls += 1
        self._write_event(_new_id(), "tool", span_id, None, {
            "function_name": function_name,
            "arguments": arguments,
            "result": result,
            "error": error,
            "duration_ms": duration_ms,
        })

    def record_info(self, span_id: str, message: str, data: dict[str, Any] | None = None) -> None:
        self._write_event(_new_id(), "info", span_id, None, {
            "message": message,
            "data": data or {},
        })

    def record_error(self, span_id: str, message: str, traceback: str | None = None) -> None:
        self._write_event(_new_id(), "error", span_id, None, {
            "message": message,
            "traceback": traceback,
        })

    def finalize(self) -> dict[str, Any]:
        """Return summary stats for updating the runs table."""
        return {
            "total_cost_usd": round(self._total_cost, 6),
            "total_input_tokens": self._total_input,
            "total_output_tokens": self._total_output,
            "total_cache_read_tokens": self._total_cache_read,
            "total_cache_write_tokens": self._total_cache_write,
            "model_call_count": self._model_calls,
            "tool_call_count": self._tool_calls,
        }

    def _write_event(
        self,
        event_id: str,
        event_type: str,
        span_id: str,
        parent_span_id: str | None,
        data: dict[str, Any],
    ) -> None:
        self.conn.execute(
            "INSERT INTO trace_events (id, run_id, event_type, span_id, parent_span_id, timestamp, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (event_id, self.run_id, event_type, span_id, parent_span_id, _now_iso(), json.dumps(data)),
        )
        self.conn.commit()
