"""Edit-and-rerun a captured LLM exchange (admin tool).

Subcommands:

    # Print the base exchange's reconstructed inputs (system prompt, message
    # stack, tool list, model). Use this to see what to edit before firing.
    uv run python scripts/exchange_forks.py show <exchange_id> [--prod]

    # Fire N parallel samples with overrides read from a JSON file.
    # Overrides JSON is partial — fields left out inherit from base.
    uv run python scripts/exchange_forks.py fire <exchange_id> \\
        --overrides overrides.json [--samples 3] [--prod]

    # List existing forks for an exchange, grouped by overrides_hash.
    uv run python scripts/exchange_forks.py list <exchange_id> [--prod]

Override JSON shape (all fields optional):

    {
      "system_prompt": "...",
      "user_messages": [{"role": "user", "content": "..."}],
      "tools": [{"name": "...", "description": "...", "input_schema": {...}}],
      "model": "claude-sonnet-4-6",
      "temperature": 0.7,
      "max_tokens": 4096
    }

Tools are a full replacement — to remove a tool, omit it; to add or edit
one, include the desired Anthropic tool dict.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
import sys
import uuid
from pathlib import Path


def _apply_env_cascade() -> None:
    """Pull the standard set of keys from .env files into os.environ.

    Without this, ``rumil.settings`` (loaded by ``rumil.forks``) reads
    pydantic-settings priority — shell env wins over .env — and a stale
    ``ANTHROPIC_API_KEY`` exported in the user's shell can silently
    redirect fork billing away from the project key.

    Mirrors the cascade the versus scripts apply at module top.
    """
    keys = (
        "ANTHROPIC_API_KEY",
        "OPENROUTER_API_KEY",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "LANGFUSE_HOST",
        "LANGFUSE_BASE_URL",
    )
    rumil_root = Path(__file__).resolve().parent.parent
    versus_root = rumil_root / "versus"
    for env_path in (versus_root / ".env", rumil_root / ".env"):
        if not env_path.is_file():
            continue
        for raw in env_path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            if k not in keys:
                continue
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                v = v[1:-1]
            os.environ[k] = v


_apply_env_cascade()


from rumil.database import DB  # noqa: E402  — env must be cascaded before rumil imports
from rumil.forks import (  # noqa: E402
    ForkOverrides,
    fire_fork,
    hash_overrides,
    resolve_base,
)

log = logging.getLogger(__name__)


def _short(s: str | None, n: int = 200) -> str:
    if s is None:
        return "<none>"
    s = s.strip()
    if len(s) <= n:
        return s
    return s[:n] + f" ... ({len(s) - n} more chars)"


async def cmd_show(args: argparse.Namespace) -> None:
    db = await DB.create(run_id=str(uuid.uuid4()), prod=args.prod)
    try:
        base = await resolve_base(db, args.exchange_id)
    finally:
        await db.close()

    print(f"exchange_id     {base.exchange_id}")
    print(f"call_id         {base.call_id}")
    print(f"call_type       {base.call_type.value if base.call_type else '<unknown>'}")
    print(f"model (default) {base.model}  (not stored on base; from settings)")
    print(f"temperature     {base.temperature}")
    print(f"max_tokens      {base.max_tokens}")
    print()
    print("system_prompt:")
    print(_short(base.system_prompt, 600))
    print()
    print(f"user_messages ({len(base.user_messages)}):")
    for i, msg in enumerate(base.user_messages):
        role = msg.get("role", "?")
        content = msg.get("content")
        if isinstance(content, str):
            preview = _short(content, 200)
        else:
            preview = f"<{len(content) if content else 0} blocks>"
        print(f"  [{i}] {role}: {preview}")
    print()
    print(f"tools ({len(base.tools)} reconstructed from call_type):")
    for tool in base.tools:
        print(f"  - {tool['name']}: {_short(tool.get('description'), 80)}")


async def cmd_fire(args: argparse.Namespace) -> None:
    overrides_path = Path(args.overrides)
    if not overrides_path.exists():
        print(f"Overrides file not found: {overrides_path}", file=sys.stderr)
        sys.exit(1)
    overrides_data = json.loads(overrides_path.read_text())
    overrides = ForkOverrides.model_validate(overrides_data)
    overrides_dict = overrides.model_dump(exclude_none=True)
    h = hash_overrides(overrides_dict)
    print(f"overrides_hash  {h}")
    print(f"overrides       {json.dumps(overrides_dict, indent=2, default=str)}")
    print(f"firing {args.samples} sample(s)...")
    print()

    db = await DB.create(run_id=str(uuid.uuid4()), prod=args.prod)
    try:
        rows = await fire_fork(
            db,
            args.exchange_id,
            overrides,
            args.samples,
            created_by=f"cli:{getpass.getuser()}",
        )
    finally:
        await db.close()

    total_cost = 0.0
    for row in rows:
        print("=" * 70)
        print(f"sample {row.sample_index}  fork_id={row.id}")
        if row.error:
            print(f"ERROR: {row.error}")
            continue
        print(
            f"model={row.model} temp={row.temperature} "
            f"in/out={row.input_tokens}/{row.output_tokens} "
            f"duration={row.duration_ms}ms cost=${row.cost_usd or 0:.4f}"
        )
        if row.cost_usd:
            total_cost += row.cost_usd
        if row.response_text:
            print()
            print(row.response_text)
        if row.tool_calls:
            print()
            print(f"tool_calls ({len(row.tool_calls)}):")
            for tc in row.tool_calls:
                print(f"  - {tc.get('name')}: {json.dumps(tc.get('input', {}))[:200]}")
    print("=" * 70)
    print(f"total cost: ${total_cost:.4f}")


async def cmd_list(args: argparse.Namespace) -> None:
    db = await DB.create(run_id=str(uuid.uuid4()), prod=args.prod)
    try:
        rows = await db.list_forks_for_exchange(args.exchange_id)
    finally:
        await db.close()

    if not rows:
        print(f"No forks for exchange {args.exchange_id}")
        return

    by_hash: dict[str, list[dict]] = {}
    for row in rows:
        by_hash.setdefault(row["overrides_hash"], []).append(row)

    print(f"{len(rows)} fork(s) across {len(by_hash)} config(s)")
    print()
    for h, group in by_hash.items():
        diff_keys = sorted(group[0]["overrides"].keys()) if group[0]["overrides"] else []
        diff_summary = ", ".join(diff_keys) if diff_keys else "<no overrides>"
        print(f"config {h}  ({len(group)} sample(s))  diffs: {diff_summary}")
        for row in sorted(group, key=lambda r: r["sample_index"]):
            err = f"  ERROR: {row['error']}" if row.get("error") else ""
            print(
                f"  [{row['sample_index']}] {row['id']}  "
                f"model={row['model']} cost=${row.get('cost_usd') or 0:.4f}"
                f"  {row['created_at']}{err}"
            )
        print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Edit and re-fire a captured LLM exchange.")
    parser.add_argument("--prod", action="store_true", help="Use production database.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_show = sub.add_parser("show", help="Print the base exchange's reconstructed inputs.")
    p_show.add_argument("exchange_id")
    p_show.set_defaults(func=cmd_show)

    p_fire = sub.add_parser("fire", help="Fire N parallel samples with overrides.")
    p_fire.add_argument("exchange_id")
    p_fire.add_argument(
        "--overrides", required=True, help="Path to JSON file with override fields."
    )
    p_fire.add_argument("--samples", type=int, default=1, help="Number of samples to fire.")
    p_fire.set_defaults(func=cmd_fire)

    p_list = sub.add_parser("list", help="List existing forks for an exchange.")
    p_list.add_argument("exchange_id")
    p_list.set_defaults(func=cmd_list)

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
