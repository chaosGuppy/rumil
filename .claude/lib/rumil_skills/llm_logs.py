"""Browse llm_boundary_exchanges — every Anthropic API exchange logged
at the transport boundary, regardless of caller (chat, llm.call_api,
structured_call, etc.).

Default mode prints a compact table of recent exchanges in the active
workspace. Filters narrow by source / model / run / call / time / error.
``--full <id>`` dumps the full request_json + response_json for one row.

Usage (run via `uv run python -m rumil_skills.llm_logs`):

    # 20 most recent in active ws
    python -m rumil_skills.llm_logs

    # Last 50 chat exchanges from a specific ws
    python -m rumil_skills.llm_logs --ws wave7-smoke --source chat --recent 50

    # Errors only in the last 2h
    python -m rumil_skills.llm_logs --error-only --since 2h

    # Full dump of one row
    python -m rumil_skills.llm_logs --full 64bcb0c7
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import UTC, datetime, timedelta

from ._format import truncate
from ._runctx import make_db

_DURATION_RE = re.compile(r"^(\d+)([smhd])$")


def _parse_since(s: str) -> datetime:
    m = _DURATION_RE.match(s.strip())
    if not m:
        raise argparse.ArgumentTypeError(f"--since must be like 30m, 2h, 1d (got {s!r})")
    n, unit = int(m.group(1)), m.group(2)
    delta = {
        "s": timedelta(seconds=n),
        "m": timedelta(minutes=n),
        "h": timedelta(hours=n),
        "d": timedelta(days=n),
    }[unit]
    return datetime.now(UTC) - delta


def _fmt_tokens(usage: dict | None) -> str:
    if not usage:
        return "-"
    inp = usage.get("input_tokens") or 0
    out = usage.get("output_tokens") or 0
    cache_r = usage.get("cache_read_input_tokens") or 0
    cache_c = usage.get("cache_creation_input_tokens") or 0
    parts = [f"{inp}/{out}"]
    if cache_r or cache_c:
        parts.append(f"c:{cache_r}/{cache_c}")
    return " ".join(parts)


def _fmt_status(row: dict) -> str:
    if row.get("error_class"):
        cls = row["error_class"]
        status = row.get("http_status")
        return f"ERR {cls}{f' [{status}]' if status else ''}"
    return row.get("stop_reason") or "-"


def _short_model(model: str) -> str:
    if model.startswith("claude-"):
        return model[len("claude-") :]
    return model


async def _table_view(args: argparse.Namespace) -> None:
    db, ws = await make_db(workspace=args.workspace)
    try:
        q = db.client.table("llm_boundary_exchanges").select(
            "id, started_at, run_id, call_id, model, source, latency_ms, "
            "usage, stop_reason, error_class, error_message, http_status, streamed"
        )
        if not args.cross_ws:
            q = q.eq("project_id", db.project_id)
        if args.source:
            q = q.like("source", f"{args.source}%")
        if args.model:
            q = q.like("model", f"%{args.model}%")
        if args.run:
            q = q.like("run_id", f"{args.run}%")
        if args.call:
            q = q.like("call_id", f"{args.call}%")
        if args.error_only:
            q = q.not_.is_("error_class", "null")
        if args.since:
            q = q.gte("started_at", _parse_since(args.since).isoformat())
        q = q.order("started_at", desc=True).limit(args.recent)
        result = await q.execute()
    finally:
        await db.close()

    rows = result.data or []
    print(f"workspace: {ws}{' (cross-ws=on)' if args.cross_ws else ''}")
    print(f"showing {len(rows)} exchanges (filters: " + _fmt_filters(args) + ")")
    if not rows:
        return
    print()
    print(
        f"{'id':<10}{'started':<22}{'source':<28}{'model':<22}"
        f"{'lat ms':>8}  {'tokens (in/out [c:r/c])':<24}{'status'}"
    )
    print("-" * 130)
    for r in rows:
        started = r["started_at"][:19].replace("T", " ")
        src = truncate(r["source"], 26)
        if r.get("streamed"):
            src = src + "*"
        print(
            f"{r['id'][:8]}  "
            f"{started:<22}"
            f"{src:<28}"
            f"{_short_model(r['model']):<22}"
            f"{(r.get('latency_ms') or 0):>8}  "
            f"{_fmt_tokens(r.get('usage')):<24}"
            f"{_fmt_status(r)}"
        )
    print()
    print("(* = streamed; --full <id> to dump full req/resp)")


def _fmt_filters(args: argparse.Namespace) -> str:
    parts = []
    if args.source:
        parts.append(f"source~{args.source}")
    if args.model:
        parts.append(f"model~{args.model}")
    if args.run:
        parts.append(f"run~{args.run}")
    if args.call:
        parts.append(f"call~{args.call}")
    if args.since:
        parts.append(f"since={args.since}")
    if args.error_only:
        parts.append("errors-only")
    parts.append(f"recent={args.recent}")
    return ", ".join(parts)


async def _full_view(args: argparse.Namespace) -> None:
    db, _ws = await make_db(workspace=args.workspace)
    try:
        q = db.client.table("llm_boundary_exchanges").select("*")
        if not args.cross_ws:
            q = q.eq("project_id", db.project_id)
        # id is UUID — postgrest can't LIKE on it, so fetch a wider window
        # ordered by recency and filter the prefix client-side.
        result = await q.order("started_at", desc=True).limit(500).execute()
    finally:
        await db.close()
    matches = [r for r in (result.data or []) if r["id"].startswith(args.full)]
    if not matches:
        print(f"no exchange matching id prefix {args.full!r} (in last 500 rows)")
        sys.exit(1)
    if len(matches) > 1:
        print(f"ambiguous prefix {args.full!r} ({len(matches)} matches); use more chars")
        for r in matches[:10]:
            print(f"  {r['id']}  {r['source']}  {r['started_at']}")
        sys.exit(1)
    r = matches[0]
    print(f"id           {r['id']}")
    print(f"source       {r['source']}{' (streamed)' if r.get('streamed') else ''}")
    print(f"model        {r['model']}")
    print(f"run_id       {r.get('run_id')}")
    print(f"call_id      {r.get('call_id')}")
    print(f"project_id   {r.get('project_id')}")
    print(f"started_at   {r['started_at']}")
    print(f"finished_at  {r.get('finished_at')}")
    print(f"latency_ms   {r.get('latency_ms')}")
    print(f"stop_reason  {r.get('stop_reason')}")
    print(f"usage        {json.dumps(r.get('usage'))}")
    if r.get("error_class"):
        print(f"error_class  {r['error_class']}")
        print(f"http_status  {r.get('http_status')}")
        print(f"error_msg    {r.get('error_message')}")
    print()
    print("=== request_json ===")
    print(json.dumps(r["request_json"], indent=2))
    print()
    print("=== response_json ===")
    print(json.dumps(r.get("response_json"), indent=2))


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Browse llm_boundary_exchanges (full Anthropic API logs)."
    )
    parser.add_argument("--workspace", default=None, help="workspace (default: session)")
    parser.add_argument(
        "--cross-ws",
        action="store_true",
        help="don't filter by workspace; show across all projects",
    )
    parser.add_argument("--source", default=None, help="prefix match on source")
    parser.add_argument("--model", default=None, help="substring match on model")
    parser.add_argument("--run", default=None, help="run_id prefix")
    parser.add_argument("--call", default=None, help="call_id prefix")
    parser.add_argument("--since", default=None, help="e.g. 30m, 2h, 1d")
    parser.add_argument("--recent", type=int, default=20, help="max rows (default 20)")
    parser.add_argument("--error-only", action="store_true")
    parser.add_argument(
        "--full",
        default=None,
        help="dump full request/response for the row with this id prefix",
    )
    args = parser.parse_args()

    if args.full:
        await _full_view(args)
    else:
        await _table_view(args)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
