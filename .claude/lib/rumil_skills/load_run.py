"""Dump every call in a rumil run by run_id.

A "run" in the frontend URL `/traces/<run_id>` is anchored on
`calls.run_id`. Sometimes there's a matching `runs` table row (for
scripted runs via `scripts/run_call.py` or the orchestrator); sometimes
there isn't (standalone dispatches just tag calls with a run_id without
registering a row). Either way, the tree view is built from the `calls`
table, grouped by `run_id` and indented by `parent_call_id`.

Usage:
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.load_run <run_id>
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.load_run <run_id> --full
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.load_run <run_id> --only llm_exchange
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.load_run <run_id> --compare <other_run_id>
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from ._format import short, trace_url
from ._runctx import make_db
from .trace import (
    _fetch_full_exchanges,
    _print_header,
    _render_event,
    _render_exchange,
)


async def _resolve_run_id(db, run_id: str) -> str | None:
    """Resolve a short (8-char) or full run ID by looking at calls.run_id."""
    if len(run_id) >= 32:
        rows = await db._execute(
            db.client.table("calls").select("run_id").eq("run_id", run_id).limit(1)
        )
        data = getattr(rows, "data", None) or []
        return data[0]["run_id"] if data else None
    # Short prefix — use a LIKE and dedupe client-side.
    rows = await db._execute(
        db.client.table("calls").select("run_id").like("run_id", f"{run_id}%").limit(50)
    )
    data = getattr(rows, "data", None) or []
    unique = sorted({r["run_id"] for r in data if r.get("run_id")})
    if len(unique) == 1:
        return unique[0]
    if len(unique) > 1:
        print(
            f"ambiguous short run id {run_id!r}: {len(unique)} matches",
            file=sys.stderr,
        )
        for rid in unique[:5]:
            print(f"  {rid}", file=sys.stderr)
        return None
    return None


async def _fetch_runs_row(db, run_id: str) -> dict[str, Any] | None:
    rows = await db._execute(db.client.table("runs").select("*").eq("id", run_id).limit(1))
    data = getattr(rows, "data", None) or []
    return data[0] if data else None


async def _fetch_project_name(db, project_id: str | None) -> str | None:
    """Resolve a project_id to its name. The runs table stores project_id
    but the user-facing label is the workspace name on the projects table."""
    if not project_id:
        return None
    rows = await db._execute(
        db.client.table("projects").select("name").eq("id", project_id).limit(1)
    )
    data = getattr(rows, "data", None) or []
    return data[0]["name"] if data else None


async def _fetch_calls_for_run(db, run_id: str) -> list[dict[str, Any]]:
    rows = await db._execute(
        db.client.table("calls").select("*").eq("run_id", run_id).order("created_at")
    )
    return list(getattr(rows, "data", None) or [])


def _order_as_tree(calls: list[dict[str, Any]]) -> list[tuple[int, dict[str, Any]]]:
    """DFS over parent_call_id, yielding (depth, call) pairs in creation order.

    Any call whose parent isn't in this run's call set is treated as a root
    so we never drop calls on the floor.
    """
    by_id = {c["id"]: c for c in calls}
    children: dict[str | None, list[dict[str, Any]]] = {}
    for c in calls:
        parent = c.get("parent_call_id")
        if parent not in by_id:
            parent = None
        children.setdefault(parent, []).append(c)
    for siblings in children.values():
        siblings.sort(key=lambda c: c.get("created_at") or "")

    out: list[tuple[int, dict[str, Any]]] = []

    def walk(parent_id: str | None, depth: int) -> None:
        for c in children.get(parent_id, []):
            out.append((depth, c))
            walk(c["id"], depth + 1)

    walk(None, 0)
    return out


# Column widths shared by _tree_header() and _call_summary_line() so the
# header row lines up with depth-0 tree rows. Deeper rows get indented
# past the header on the left — that's the point of the tree layout.
_COL_ID = 8
_COL_TYPE = 18
_COL_STATUS = 10
_COL_COST = 8
_COL_SCOPE = 14  # "scope=" + 8-char short id


def _tree_header() -> str:
    return (
        f"{'id':<{_COL_ID}}  "
        f"{'type':<{_COL_TYPE}}  "
        f"{'status':<{_COL_STATUS}}  "
        f"{'cost':>{_COL_COST}}  "
        f"{'scope':<{_COL_SCOPE}}  "
        "created"
    )


def _call_summary_line(depth: int, call: dict[str, Any]) -> str:
    indent = "  " * depth
    cost = call.get("cost_usd")
    cost_s = f"${cost:.3f}" if cost is not None else "—"
    created = (call.get("created_at") or "")[:19].replace("T", " ")
    ct = call.get("call_type", "?")
    status = call.get("status", "?")
    scope_id = short(call.get("scope_page_id") or "")
    scope_s = f"scope={scope_id}" if scope_id else ""
    return (
        f"{indent}{short(call['id']):<{_COL_ID}}  "
        f"{ct:<{_COL_TYPE}}  "
        f"{status:<{_COL_STATUS}}  "
        f"{cost_s:>{_COL_COST}}  "
        f"{scope_s:<{_COL_SCOPE}}  "
        f"{created}"
    )


def _event_summary(events: list[dict[str, Any]], only: str | None) -> str:
    """One-line-ish summary of the events for a call — counts + highlights."""
    if not events:
        return "    (no events)"
    counts: dict[str, int] = {}
    for e in events:
        counts[e.get("event", "?")] = counts.get(e.get("event", "?"), 0) + 1
    parts = [f"{k}×{v}" for k, v in sorted(counts.items())]
    head = "    events: " + " ".join(parts)

    interesting = [e for e in events if e.get("event") in {"error", "warning"}]
    if only:
        interesting = [e for e in events if e.get("event") == only]
    if not interesting:
        return head
    lines = [head]
    for e in interesting[:5]:
        lines.append(_render_event(e, brief=True))
    if len(interesting) > 5:
        lines.append(f"    … +{len(interesting) - 5} more")
    return "\n".join(lines)


def _result_summary(call: dict[str, Any], events: list[dict[str, Any]]) -> str:
    """One-line condensed summary of a call's outcome — events + cost + status.

    Used by --compare mode where full exchanges aren't rendered. Surfaces
    the high-signal counts: how many llm exchanges, moves, errors, plus
    review_complete fruit/confidence if present.
    """
    counts: dict[str, int] = {}
    review = None
    err = None
    for e in events:
        ev = e.get("event", "?")
        counts[ev] = counts.get(ev, 0) + 1
        if ev == "review_complete":
            review = e
        if ev == "error" and err is None:
            err = e.get("message") or "?"
    bits: list[str] = []
    for k in ("llm_exchange", "moves_executed", "view_created", "dispatch_executed"):
        if k in counts:
            bits.append(f"{k.split('_')[0]}×{counts[k]}")
    if review:
        bits.append(f"fruit={review.get('remaining_fruit')} conf={review.get('confidence')}")
    if err:
        bits.append(f"err={err[:40]}")
    return " ".join(bits) or "(no events)"


def _align_calls_by_type(
    calls_a: list[dict[str, Any]],
    calls_b: list[dict[str, Any]],
) -> list[tuple[dict[str, Any] | None, dict[str, Any] | None]]:
    """Align two run's calls by call_type, preserving creation order within each type.

    Walks call_types in the order they first appear across both runs (union,
    A's order first, then any B-only types appended). Within each type,
    pairs Nth-of-type-in-A with Nth-of-type-in-B; extras in either run land
    as (call, None) or (None, call) rows. This handles the "3 scouts vs 2
    scouts" case cleanly without trying to reason about parent/child
    structure (which can legitimately differ between runs).
    """
    by_type_a: dict[str, list[dict[str, Any]]] = {}
    by_type_b: dict[str, list[dict[str, Any]]] = {}
    for c in sorted(calls_a, key=lambda x: x.get("created_at") or ""):
        by_type_a.setdefault(c.get("call_type", "?"), []).append(c)
    for c in sorted(calls_b, key=lambda x: x.get("created_at") or ""):
        by_type_b.setdefault(c.get("call_type", "?"), []).append(c)

    seen: set[str] = set()
    ordered_types: list[str] = []
    for c in sorted(calls_a, key=lambda x: x.get("created_at") or ""):
        ct = c.get("call_type", "?")
        if ct not in seen:
            ordered_types.append(ct)
            seen.add(ct)
    for c in sorted(calls_b, key=lambda x: x.get("created_at") or ""):
        ct = c.get("call_type", "?")
        if ct not in seen:
            ordered_types.append(ct)
            seen.add(ct)

    rows: list[tuple[dict[str, Any] | None, dict[str, Any] | None]] = []
    for ct in ordered_types:
        a_list = by_type_a.get(ct, [])
        b_list = by_type_b.get(ct, [])
        n = max(len(a_list), len(b_list))
        for i in range(n):
            a = a_list[i] if i < len(a_list) else None
            b = b_list[i] if i < len(b_list) else None
            rows.append((a, b))
    return rows


def _compare_cell(call: dict[str, Any] | None, events: list[dict[str, Any]] | None) -> str:
    """Render one side of a comparison row — short id + status + cost + summary."""
    if call is None:
        return f"{'—':<60}"
    cost = call.get("cost_usd")
    cost_s = f"${cost:.3f}" if cost is not None else "—"
    status = call.get("status", "?")
    summary = _result_summary(call, events or [])
    return f"{short(call['id'])} {status:<8} {cost_s:>7}  {summary}"[:90]


def _print_compare(
    run_a: str,
    calls_a: list[dict[str, Any]],
    events_a: dict[str, list[dict[str, Any]]],
    runs_row_a: dict[str, Any] | None,
    run_b: str,
    calls_b: list[dict[str, Any]],
    events_b: dict[str, list[dict[str, Any]]],
    runs_row_b: dict[str, Any] | None,
) -> None:
    print("=== compare ===")
    name_a = (runs_row_a or {}).get("name") or "—"
    name_b = (runs_row_b or {}).get("name") or "—"
    total_a = sum(c.get("cost_usd") or 0 for c in calls_a)
    total_b = sum(c.get("cost_usd") or 0 for c in calls_b)
    print(f"A: {short(run_a)}  {name_a}  calls={len(calls_a)}  total=${total_a:.3f}")
    print(f"B: {short(run_b)}  {name_b}  calls={len(calls_b)}  total=${total_b:.3f}")
    print()

    rows = _align_calls_by_type(calls_a, calls_b)

    print(f"{'call_type':<22}  {'A':<60}  {'B':<60}  diff")
    last_type: str | None = None
    for a, b in rows:
        ct = (a or b or {}).get("call_type", "?")
        cell_a = _compare_cell(a, events_a.get(a["id"]) if a else None)
        cell_b = _compare_cell(b, events_b.get(b["id"]) if b else None)
        diff_marks: list[str] = []
        if a is None or b is None:
            diff_marks.append("MISSING")
        else:
            if a.get("status") != b.get("status"):
                diff_marks.append("status")
            ca, cb = a.get("cost_usd") or 0, b.get("cost_usd") or 0
            if ca and cb and abs(ca - cb) / max(ca, cb) > 0.25:
                diff_marks.append(f"cost {ca:.3f}vs{cb:.3f}")
            ev_a = events_a.get(a["id"]) or []
            ev_b = events_b.get(b["id"]) or []
            xa = sum(1 for e in ev_a if e.get("event") == "llm_exchange")
            xb = sum(1 for e in ev_b if e.get("event") == "llm_exchange")
            if xa != xb:
                diff_marks.append(f"exchanges {xa}vs{xb}")
        marker = "*" if diff_marks else " "
        type_label = ct if ct != last_type else ""
        last_type = ct
        print(f"{type_label:<22}  {cell_a:<60}  {cell_b:<60}  {marker} {' '.join(diff_marks)}")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_id", help="Full or short (8+ char) run ID")
    parser.add_argument("--workspace", default=None)
    parser.add_argument(
        "--full",
        action="store_true",
        help="Print every call's full trace (events + verbatim exchanges)",
    )
    parser.add_argument(
        "--only",
        default=None,
        help="Filter per-call event summary to this event name",
    )
    parser.add_argument(
        "--last-n",
        type=int,
        default=None,
        help="In --full mode, trim each call to its last N exchanges",
    )
    parser.add_argument(
        "--compare",
        default=None,
        metavar="OTHER_RUN_ID",
        help=(
            "Show a side-by-side comparison with another run, aligned by "
            "call_type. No verbatim exchanges in compare mode — call summaries only."
        ),
    )
    args = parser.parse_args()

    db, ws = await make_db(workspace=args.workspace)
    try:
        full_id = await _resolve_run_id(db, args.run_id)
        if not full_id:
            print(f"no calls with run_id matching {args.run_id!r}")
            sys.exit(1)

        runs_row = await _fetch_runs_row(db, full_id)
        run_workspace = await _fetch_project_name(db, (runs_row or {}).get("project_id"))
        calls = await _fetch_calls_for_run(db, full_id)
        # trace_json is a JSONB column on calls, already hydrated by
        # _fetch_calls_for_run's select("*") — no extra round trips.
        events_by_call = {c["id"]: (c.get("trace_json") or []) for c in calls}
        exchanges_by_call: dict[str, list[dict[str, Any]]] = {}
        if args.full:
            for c in calls:
                exchanges_by_call[c["id"]] = await _fetch_full_exchanges(db, c["id"])

        compare_id: str | None = None
        compare_calls: list[dict[str, Any]] = []
        compare_events: dict[str, list[dict[str, Any]]] = {}
        compare_runs_row: dict[str, Any] | None = None
        if args.compare:
            compare_id = await _resolve_run_id(db, args.compare)
            if not compare_id:
                print(f"no calls with run_id matching {args.compare!r}")
                sys.exit(1)
            compare_runs_row = await _fetch_runs_row(db, compare_id)
            compare_calls = await _fetch_calls_for_run(db, compare_id)
            compare_events = {c["id"]: (c.get("trace_json") or []) for c in compare_calls}
    finally:
        await db.close()

    if run_workspace and run_workspace != ws:
        print(f"workspace: {run_workspace}  (session active: {ws})")
    else:
        print(f"workspace: {run_workspace or ws}")
    print(f"run:       {full_id}")
    print(f"trace url: {trace_url(full_id)}")
    if runs_row:
        print(f"name:      {runs_row.get('name') or '—'}")
        print(f"question:  {runs_row.get('question_id') or '—'}")
        cfg = runs_row.get("config") or {}
        origin = cfg.get("origin") if isinstance(cfg, dict) else None
        skill = cfg.get("skill") if isinstance(cfg, dict) else None
        if origin or skill:
            print(f"origin:    {origin or '—'}  skill={skill or '—'}")
    else:
        print("runs row:  (none — standalone dispatch)")
    print(f"calls:     {len(calls)}")
    print()

    if not calls:
        return

    if compare_id:
        _print_compare(
            full_id,
            calls,
            events_by_call,
            runs_row,
            compare_id,
            compare_calls,
            compare_events,
            compare_runs_row,
        )
        return

    print("=== call tree ===")
    print(_tree_header())
    for depth, call in _order_as_tree(calls):
        print(_call_summary_line(depth, call))
        print(_event_summary(events_by_call.get(call["id"], []), args.only))
    print()

    if not args.full:
        print("tip: drill into one call with  rumil-trace <short_id>")
        return

    print("=== full per-call traces ===")
    for _, call in _order_as_tree(calls):
        print()
        print(f"### call {short(call['id'])}  {call.get('call_type')}  {call.get('status')}")
        _print_header(call, full_id)
        events = events_by_call.get(call["id"], [])
        if args.only:
            events = [e for e in events if e.get("event") == args.only]
        for ev in events:
            print(_render_event(ev, brief=False))
        exchanges = exchanges_by_call.get(call["id"], [])
        if args.last_n:
            exchanges = exchanges[-args.last_n :]
        if exchanges:
            print()
            print("--- llm exchanges (verbatim) ---")
            for i, ex in enumerate(exchanges, start=1):
                print(_render_exchange(ex, i, brief=False))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
