"""Backfill prompt_versions + composite_prompt_hash for pre-versioning exchanges.

Streams ``call_llm_exchanges`` rows in batches, computes a content-hash
on the pre-date-suffix system_prompt, upserts a ``prompt_versions`` row,
and stamps ``composite_prompt_hash`` on the exchange. Also sets
``calls.primary_prompt_hash`` / ``primary_prompt_name`` from the first
non-closing-review exchange per call.

Usage::

    uv run python scripts/backfill_prompt_versions.py
    uv run python scripts/backfill_prompt_versions.py --prod  # careful

Idempotent — rerunning on already-backfilled rows is a no-op (the RPC
dedups by hash; the exchange update stays a no-op if the hash is
already stamped).
"""

import argparse
import asyncio
import logging
from collections.abc import Sequence

from rumil.database import DB
from rumil.db.call_store import _hash_prompt_content, _strip_date_suffix
from rumil.settings import get_settings

log = logging.getLogger("backfill_prompt_versions")


async def _fetch_batch(db: DB, offset: int, limit: int) -> Sequence[dict]:
    result = await db._execute(
        db.client.table("call_llm_exchanges")
        .select("id,call_id,phase,system_prompt,composite_prompt_hash")
        .not_.is_("system_prompt", "null")
        .is_("composite_prompt_hash", "null")
        .order("created_at")
        .range(offset, offset + limit - 1)
    )
    return list(result.data or [])


async def _stamp_exchange(db: DB, exchange_id: str, hash_: str) -> None:
    await db._execute(
        db.client.table("call_llm_exchanges")
        .update({"composite_prompt_hash": hash_})
        .eq("id", exchange_id)
    )


async def _upsert_prompt(db: DB, hash_: str, content: str, name: str) -> None:
    await db._execute(
        db.client.rpc(
            "upsert_prompt_version",
            {
                "p_hash": hash_,
                "p_name": name,
                "p_content": content,
                "p_kind": "composite_legacy",
            },
        )
    )


async def _primary_hash_for_call(db: DB, call_id: str) -> tuple[str, str] | None:
    rows = await db._execute(
        db.client.table("call_llm_exchanges")
        .select("phase,composite_prompt_hash,round")
        .eq("call_id", call_id)
        .not_.is_("composite_prompt_hash", "null")
        .order("round")
    )
    data = list(rows.data or [])
    if not data:
        return None
    agent = [r for r in data if not str(r.get("phase", "")).startswith("closing_review")]
    chosen = agent[0] if agent else data[0]
    call_rows = await db._execute(
        db.client.table("calls").select("call_type").eq("id", call_id).limit(1)
    )
    call_data = list(call_rows.data or [])
    name = call_data[0]["call_type"] if call_data else "unknown"
    return chosen["composite_prompt_hash"], name


async def run(prod: bool, batch_size: int, limit: int | None) -> None:
    db = await DB.create(run_id="backfill-prompt-versions", prod=prod)
    try:
        offset = 0
        seen_hashes: set[str] = set()
        touched_calls: set[str] = set()
        while True:
            batch = await _fetch_batch(db, offset, batch_size)
            if not batch:
                break
            for row in batch:
                content = row.get("system_prompt") or ""
                if not content:
                    continue
                pre = _strip_date_suffix(content)
                h = _hash_prompt_content(pre)
                if h not in seen_hashes:
                    await _upsert_prompt(db, h, pre, name="<unknown-legacy>")
                    seen_hashes.add(h)
                await _stamp_exchange(db, row["id"], h)
                if row.get("call_id"):
                    touched_calls.add(row["call_id"])
            offset += len(batch)
            log.info("exchange batch offset=%d done (cum=%d)", offset - len(batch), offset)
            if limit and offset >= limit:
                break

        log.info("stamping primary_prompt_hash on %d calls", len(touched_calls))
        stamped = 0
        for call_id in touched_calls:
            primary = await _primary_hash_for_call(db, call_id)
            if primary is None:
                continue
            await db._execute(
                db.client.table("calls")
                .update(
                    {
                        "primary_prompt_hash": primary[0],
                        "primary_prompt_name": primary[1],
                    }
                )
                .eq("id", call_id)
                .is_("primary_prompt_hash", "null")
            )
            stamped += 1
        log.info(
            "done; exchanges=%d unique_hashes=%d calls_stamped=%d",
            offset,
            len(seen_hashes),
            stamped,
        )
    finally:
        await db.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--prod", action="store_true", help="Run against the production database.")
    ap.add_argument("--batch-size", type=int, default=500)
    ap.add_argument("--limit", type=int, default=None, help="Stop after this many exchanges.")
    args = ap.parse_args()
    get_settings()  # fail-fast if env is broken
    asyncio.run(run(prod=args.prod, batch_size=args.batch_size, limit=args.limit))


if __name__ == "__main__":
    main()
