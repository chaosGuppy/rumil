"""List recent runs in the active rumil workspace.

Solves the "what's the status of the N runs I just fired" question that
shows up whenever an iterate / A/B / batch script fans runs out in
parallel. The existing skills cover single calls (`rumil-trace`), single
runs (`rumil-load-run`), and quality scans (`rumil-find-confusion`) —
nothing for "show me the last N runs and which are done".

Usage::

    # Last N runs in active workspace (default 20)
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.list_runs

    # Filter by name substring (case-insensitive)
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.list_runs \\
        --name versus-orch-completion

    # Postgres LIKE pattern (use %)
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.list_runs \\
        --like 'versus-orch-completion:versus:forethought__%'

    # Filter by status
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.list_runs \\
        --status running

    # Aggregate-only summary (counts + total cost)
    PYTHONPATH=.claude/lib uv run python -m rumil_skills.list_runs \\
        --like 'versus-orch-completion:%' --summary

Notes:
  - ``runs.cost_usd_cents`` is in cents, not dollars (renamed in display).
  - ``started_at`` / ``finished_at`` may be NULL (run created but not yet
    started, or in-flight). Display falls back to a dash.
  - Active workspace comes from ``.claude/state/rumil-session.json`` —
    override with ``--workspace``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from ._format import truncate
from ._runctx import make_db


def _fmt_status(status: str | None) -> str:
    return (status or "?")[:9]


def _fmt_ts(ts: object) -> str:
    """Truncate ISO timestamps to 'YYYY-MM-DD HH:MM' or '—' for NULL."""
    if not ts:
        return "—"
    s = str(ts)
    return s[:16].replace("T", " ")


def _fmt_cost(cents: int | None) -> str:
    if cents is None:
        return "—"
    return f"${cents / 100:.2f}"


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default=None, help="Override active workspace.")
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Max rows to return (default 20).",
    )
    parser.add_argument(
        "--name",
        default=None,
        help="Case-insensitive substring match against runs.name (Postgres ILIKE).",
    )
    parser.add_argument(
        "--like",
        default=None,
        help="Postgres LIKE pattern against runs.name (use %% wildcard). Mutually exclusive with --name.",
    )
    parser.add_argument(
        "--status",
        default=None,
        help="Filter by exact status (e.g. running, complete, error).",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print aggregate counts + total cost only; skip the per-run table.",
    )
    args = parser.parse_args()

    if args.name and args.like:
        parser.error("--name and --like are mutually exclusive")

    db, ws = await make_db(workspace=args.workspace)
    try:
        q = (
            db.client.table("runs")
            .select("id,name,status,started_at,finished_at,cost_usd_cents,question_id")
            .eq("project_id", db.project_id)
        )

        if args.like:
            q = q.like("name", args.like)
        elif args.name:
            q = q.ilike("name", f"%{args.name}%")
        if args.status:
            q = q.eq("status", args.status)

        q = q.order("started_at", desc=True).limit(args.limit)
        res = await db._execute(q)
        rows = res.data or []
    finally:
        await db.close()

    print(f"workspace: {ws}")
    if not rows:
        print("(no matching runs)")
        return

    if args.summary:
        from collections import Counter

        statuses = Counter(r.get("status") or "?" for r in rows)
        total_cents = sum((r.get("cost_usd_cents") or 0) for r in rows)
        print(f"{len(rows)} run(s) matched (limit={args.limit}):")
        for status, n in sorted(statuses.items(), key=lambda kv: -kv[1]):
            print(f"  {_fmt_status(status):9s}  {n:4d}")
        print(f"  total cost: ${total_cents / 100:.2f}")
        return

    print(f"{len(rows)} run(s):")
    print(f"  {'id':8s}  {'status':9s}  {'started':16s}  {'finished':16s}  {'cost':>7s}  name")
    for r in rows:
        rid = (r["id"] or "")[:8]
        status = _fmt_status(r.get("status"))
        started = _fmt_ts(r.get("started_at"))
        finished = _fmt_ts(r.get("finished_at"))
        cost = _fmt_cost(r.get("cost_usd_cents"))
        name = truncate(r.get("name") or "", 80)
        print(f"  {rid}  {status:9s}  {started:16s}  {finished:16s}  {cost:>7s}  {name}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
