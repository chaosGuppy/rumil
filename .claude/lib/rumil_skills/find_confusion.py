"""Scan recent rumil calls for signs of model confusion.

Two modes:

**Heuristic (default)**: fast, free. Scores recent calls by hard signals
(errors in trace, non-complete status, exchange errors) and soft signals
(short output relative to input, cost outliers, long duration). Prints
a ranked list; no LLM calls.

**Deep (--deep)**: for the top-N heuristic candidates, runs a meta LLM
call with the shared ``confusion_scan_system.md`` system prompt and the
full trace in the user message. Returns a structured verdict per call,
persisted to ``.claude/state/rumil-scan-log.json`` so re-runs don't
re-pay for the same calls.

Usage:
    # Heuristic only, last 20 calls
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.find_confusion

    # Deep scan top 5 candidates from the last 40 calls, cheap model
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.find_confusion \\
        --limit 40 --deep --deep-limit 5

    # Force re-scan even if already in the log
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.find_confusion \\
        --deep --force-rescan

    # Override meta-model
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.find_confusion \\
        --deep --model claude-haiku-4-5-20251001
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from ._format import short, truncate
from ._runctx import make_db
from .llm_helpers import (
    DEFAULT_META_MODEL,
    load_prompt,
    meta_structured_call,
)
from .scan_log import (
    filter_unscanned,
    is_scanned,
    load_scan_log,
    record_scan,
    save_scan_log,
)

HEURISTIC_RESPONSE_SHORT_THRESHOLD = 200  # chars
HEURISTIC_INPUT_LARGE_THRESHOLD = 2000  # chars
COST_OUTLIER_MULTIPLIER = 3.0  # cost > median * 3 → flag


@dataclass
class HeuristicSignal:
    name: str
    severity: int  # 1 (weak) to 5 (strong)
    detail: str


@dataclass
class HeuristicResult:
    call_id: str
    call_type: str
    status: str
    cost_usd: float | None
    created_at: str
    signals: list[HeuristicSignal]
    score: int  # sum of signal severities

    @property
    def short_id(self) -> str:
        return self.call_id[:8]


class ConfusionVerdict(BaseModel):
    """Structured output of a deep LLM scan of one trace."""

    verdict: str = Field(
        description="'confused', 'ok', or 'inconclusive'",
    )
    severity: int | None = Field(
        default=None,
        description=("1-5, meaningful only when verdict='confused'. Null otherwise."),
    )
    primary_symptom: str = Field(
        default="",
        description=(
            "Single most load-bearing symptom label (e.g. 'scope_drift', "
            "'thin_output', 'tool_misuse')."
        ),
    )
    evidence: list[str] = Field(
        default_factory=list,
        description="1-3 short verbatim quotes with exchange numbers",
    )
    suggested_action: str = Field(
        default="",
        description="One of: 'inspect', 'redispatch', 'edit_prompt:<file>', 'ignore'",
    )


async def _fetch_recent_calls(db, limit: int) -> list[dict[str, Any]]:
    """Fetch recent calls in the current project, ordered most-recent first."""
    query = (
        db.client.table("calls")
        .select(
            "id,call_type,status,cost_usd,created_at,completed_at,"
            "trace_json,scope_page_id"
        )
        .order("created_at", desc=True)
        .limit(limit)
    )
    if db.project_id:
        query = query.eq("project_id", db.project_id)
    rows = await db._execute(query)
    return list(getattr(rows, "data", None) or [])


async def _fetch_exchanges(db, call_id: str) -> list[dict[str, Any]]:
    rows = await db._execute(
        db.client.table("call_llm_exchanges")
        .select("*")
        .eq("call_id", call_id)
        .order("round")
        .order("created_at")
    )
    return list(getattr(rows, "data", None) or [])


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 0:
        return (s[mid - 1] + s[mid]) / 2
    return s[mid]


async def _score_heuristics(db, calls: list[dict[str, Any]]) -> list[HeuristicResult]:
    """Assign heuristic signals + scores to each call."""
    costs = [c["cost_usd"] for c in calls if c.get("cost_usd") is not None]
    cost_median = _median(costs)
    results: list[HeuristicResult] = []

    for c in calls:
        signals: list[HeuristicSignal] = []

        # Skip claude_code_direct envelopes — no rumil-internal LLM loop.
        if c.get("call_type") == "claude_code_direct":
            continue

        # Hard: status
        status = c.get("status") or ""
        if status in ("failed", "running"):
            signals.append(
                HeuristicSignal(
                    name="non_complete_status",
                    severity=5,
                    detail=f"status={status}",
                )
            )
        elif status != "complete":
            signals.append(
                HeuristicSignal(
                    name="unknown_status",
                    severity=2,
                    detail=f"status={status!r}",
                )
            )

        # Hard: error events in trace_json
        trace = c.get("trace_json") or []
        error_events = [e for e in trace if e.get("event") == "error"]
        if error_events:
            msg = truncate(error_events[0].get("message", ""), 60)
            signals.append(
                HeuristicSignal(
                    name="trace_error",
                    severity=5,
                    detail=f"{len(error_events)}x: {msg}",
                )
            )

        warning_events = [e for e in trace if e.get("event") == "warning"]
        if len(warning_events) >= 2:
            signals.append(
                HeuristicSignal(
                    name="multiple_warnings",
                    severity=2,
                    detail=f"{len(warning_events)} warnings",
                )
            )

        # Soft: cost outlier
        cost = c.get("cost_usd")
        if (
            cost is not None
            and cost_median > 0
            and cost > cost_median * COST_OUTLIER_MULTIPLIER
        ):
            signals.append(
                HeuristicSignal(
                    name="cost_outlier",
                    severity=2,
                    detail=f"${cost:.3f} vs ${cost_median:.3f} median",
                )
            )

        # Soft: exchange-level signals (requires a second query per call)
        exchanges = await _fetch_exchanges(db, c["id"])
        exchange_errors = [ex for ex in exchanges if ex.get("error")]
        if exchange_errors:
            signals.append(
                HeuristicSignal(
                    name="exchange_error",
                    severity=4,
                    detail=f"{len(exchange_errors)} failed exchange(s)",
                )
            )

        # Short response relative to big input
        for ex in exchanges:
            in_len = len(ex.get("user_message") or "")
            out_len = len(ex.get("response_text") or "")
            if (
                in_len > HEURISTIC_INPUT_LARGE_THRESHOLD
                and out_len < HEURISTIC_RESPONSE_SHORT_THRESHOLD
                and not ex.get("tool_calls")
            ):
                signals.append(
                    HeuristicSignal(
                        name="thin_output",
                        severity=2,
                        detail=(
                            f"exchange round={ex.get('round')} "
                            f"in={in_len} out={out_len}"
                        ),
                    )
                )
                break  # one is enough to flag

        if signals:
            score = sum(s.severity for s in signals)
            results.append(
                HeuristicResult(
                    call_id=c["id"],
                    call_type=c.get("call_type") or "?",
                    status=status,
                    cost_usd=cost,
                    created_at=c.get("created_at") or "",
                    signals=signals,
                    score=score,
                )
            )

    results.sort(key=lambda r: r.score, reverse=True)
    return results


def _format_trace_for_llm(
    call_row: dict[str, Any],
    exchanges: list[dict[str, Any]],
) -> str:
    """Render a call + its exchanges for the confusion-scan LLM.

    This is the user_message content; the big static scanner system
    prompt is in prompts/confusion_scan_system.md.
    """
    parts: list[str] = []
    parts.append("# Trace to scan")
    parts.append("")
    parts.append(f"- call id: {call_row['id']}")
    parts.append(f"- call type: {call_row.get('call_type', '?')}")
    parts.append(f"- status: {call_row.get('status', '?')}")
    cost = call_row.get("cost_usd")
    parts.append(f"- cost: ${cost:.3f}" if cost is not None else "- cost: —")

    trace = call_row.get("trace_json") or []
    if trace:
        parts.append("")
        parts.append("## Trace events")
        for ev in trace:
            name = ev.get("event", "?")
            if name == "llm_exchange":
                continue  # exchanges rendered in full below
            compact = {
                k: v for k, v in ev.items() if k not in {"event", "ts", "call_id"}
            }
            parts.append(f"- {name}: {truncate(str(compact), 140)}")

    parts.append("")
    parts.append("## LLM exchanges")
    if not exchanges:
        parts.append("(none)")
    for i, ex in enumerate(exchanges, start=1):
        parts.append("")
        parts.append(
            f"### exchange {i}  phase={ex.get('phase')!r}  round={ex.get('round')}"
        )
        if ex.get("error"):
            parts.append(f"ERROR: {ex['error']}")
        if ex.get("user_message"):
            parts.append("user_message:")
            parts.append(truncate(ex["user_message"], 4000))
        if ex.get("response_text"):
            parts.append("response_text:")
            parts.append(truncate(ex["response_text"], 4000))
        tool_calls = ex.get("tool_calls")
        if tool_calls:
            parts.append(f"tool_calls: {truncate(str(tool_calls), 1200)}")

    parts.append("")
    parts.append(
        "Return a single ConfusionVerdict JSON object judging whether this "
        "trace shows confusion. Be specific and terse."
    )
    return "\n".join(parts)


async def _deep_scan_one(
    db,
    call_row: dict[str, Any],
    *,
    system_prompt: str,
    model: str,
) -> ConfusionVerdict | None:
    exchanges = await _fetch_exchanges(db, call_row["id"])
    user_message = _format_trace_for_llm(call_row, exchanges)
    try:
        result = await meta_structured_call(
            system_prompt=system_prompt,
            user_message=user_message,
            response_model=ConfusionVerdict,
            model=model,
        )
    except Exception as e:
        print(f"  deep scan failed for {call_row['id'][:8]}: {e}", file=sys.stderr)
        return None
    return result.parsed


def _print_heuristic_result(r: HeuristicResult) -> None:
    created = r.created_at[:19].replace("T", " ")
    cost_s = f"${r.cost_usd:.3f}" if r.cost_usd is not None else "     "
    print(
        f"  [{r.score:3d}] {r.short_id}  {created}  {cost_s}  "
        f"{r.status:8}  {r.call_type}"
    )
    for s in r.signals:
        print(f"         · {s.name} [{s.severity}] {s.detail}")


def _print_deep_verdict(call_id: str, verdict: ConfusionVerdict) -> None:
    short = call_id[:8]
    v = verdict.verdict
    sev = f" s{verdict.severity}" if verdict.severity is not None else ""
    print(f"  {short}  [{v}{sev}]  {verdict.primary_symptom or '—'}")
    for ev in verdict.evidence[:3]:
        print(f"           · {ev}")
    if verdict.suggested_action:
        print(f"           → {verdict.suggested_action}")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default=None)
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="How many recent calls to consider",
    )
    parser.add_argument(
        "--deep",
        action="store_true",
        help="LLM-scan the top heuristic candidates",
    )
    parser.add_argument(
        "--deep-limit",
        type=int,
        default=5,
        help="Max calls to deep-scan per run",
    )
    parser.add_argument(
        "--model",
        default=None,
        help=f"Meta-model for deep scans (default {DEFAULT_META_MODEL})",
    )
    parser.add_argument(
        "--force-rescan",
        action="store_true",
        help="Re-scan calls that are already in the scan log",
    )
    parser.add_argument(
        "--structural",
        metavar="QUESTION_ID",
        default=None,
        help="Run graph health checks on a question's subtree (no LLM cost)",
    )
    args = parser.parse_args()

    db, ws = await make_db(workspace=args.workspace)
    try:
        if args.structural:
            from .scan import collect_subtree, format_findings, graph_health

            full_id = await db.resolve_page_id(args.structural)
            if not full_id:
                print(f"no question matching {args.structural!r} in workspace {ws!r}")
                sys.exit(1)
            question = await db.get_page(full_id)
            if question is None:
                print(f"page {short(full_id)} vanished mid-lookup")
                sys.exit(1)
            print(f"workspace: {ws}")
            print(f"question:  {short(full_id)}  {truncate(question.headline, 80)}")
            print()
            data = await collect_subtree(db, full_id)
            findings = graph_health(data)
            actionable = [f for f in findings if f.severity > 0]
            print(f"=== structural health ({len(actionable)} findings) ===")
            print(format_findings(findings) if findings else "  (clean)")
            return

        calls = await _fetch_recent_calls(db, args.limit)
        print(f"workspace: {ws}")
        print(f"scanned:   {len(calls)} recent calls")
        heuristic = await _score_heuristics(db, calls)
        print(f"flagged:   {len(heuristic)} by heuristics")
        print()

        if not heuristic:
            print("no heuristic flags — all recent calls look nominal")
            return

        print("=== heuristic flags (ranked) ===")
        for r in heuristic:
            _print_heuristic_result(r)
        print()

        if not args.deep:
            print(
                "(pass --deep to LLM-scan the top candidates for structured "
                "confusion verdicts)"
            )
            return

        log = load_scan_log()
        if args.force_rescan:
            candidate_ids = [r.call_id for r in heuristic[: args.deep_limit]]
        else:
            remaining = filter_unscanned(log, [r.call_id for r in heuristic])
            candidate_ids = remaining[: args.deep_limit]

        cached_hits = [
            r
            for r in heuristic
            if is_scanned(log, r.call_id) and r.call_id not in candidate_ids
        ]

        if cached_hits:
            print("=== previously scanned (cached) ===")
            for r in cached_hits:
                prior = log["calls"][r.call_id]
                v = prior.get("verdict", "?")
                sev = prior.get("severity")
                sev_s = f" s{sev}" if sev is not None else ""
                sym = prior.get("primary_symptom") or "—"
                print(f"  {r.short_id}  [{v}{sev_s}]  {sym}")
            print()

        if not candidate_ids:
            print("no new candidates to deep-scan")
            return

        print(f"=== deep scanning {len(candidate_ids)} call(s) ===")
        system_prompt = load_prompt("confusion_scan_system.md")
        model = args.model or DEFAULT_META_MODEL

        # Keep a by-id map for quick re-lookup
        calls_by_id = {c["id"]: c for c in calls}

        for cid in candidate_ids:
            call_row = calls_by_id.get(cid)
            if not call_row:
                continue
            verdict = await _deep_scan_one(
                db, call_row, system_prompt=system_prompt, model=model
            )
            if verdict is None:
                continue
            record_scan(
                log,
                cid,
                model=model,
                verdict=verdict.verdict,
                severity=verdict.severity,
                primary_symptom=verdict.primary_symptom,
                evidence=list(verdict.evidence),
                suggested_action=verdict.suggested_action,
            )
            save_scan_log(log)
            _print_deep_verdict(cid, verdict)
    finally:
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
