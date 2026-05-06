"""Use-intensity counts shared across atlas indices.

Three signals, batched into one DB pass:

- ``call_type_recent``: count of recent exchanges per call_type, taken
  from the ``scan`` most-recent ``call_llm_exchanges`` rows joined to
  their parent call.
- ``call_type_lifetime``: all-time call count per ``CallType`` (one
  ``count("exact")`` per type, parallel via ``asyncio.gather``).
- ``move_recent``: count of move executions per ``MoveType`` across
  the recent ``calls_scan`` calls, walked from ``trace_json``.
- ``page_type_lifetime``: all-time count per ``PageType``.

Indices that want intensity bars (/atlas/calls, /atlas/dispatches,
/atlas/moves, /atlas/pages) call these helpers and join the results
into the relevant summary list.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any

from postgrest.types import CountMethod

from rumil.atlas import event_keys
from rumil.database import DB
from rumil.models import CallType, MoveType, PageType


async def call_type_recent_counts(db: DB, *, scan: int = 2000) -> dict[str, int]:
    res = await db._execute(
        db.client.table("call_llm_exchanges")
        .select("call_id")
        .order("created_at", desc=True)
        .limit(scan)
    )
    raw = list(res.data or [])
    if not raw:
        return {}
    call_ids = list({str(r.get("call_id")) for r in raw if r.get("call_id")})
    if not call_ids:
        return {}
    cres = await db._execute(db.client.table("calls").select("id, call_type").in_("id", call_ids))
    by_id = {str(c.get("id") or ""): str(c.get("call_type") or "") for c in (cres.data or [])}
    out: dict[str, int] = {}
    for r in raw:
        ct = by_id.get(str(r.get("call_id") or ""), "")
        if ct:
            out[ct] = out.get(ct, 0) + 1
    return out


async def call_type_lifetime_counts(db: DB) -> dict[str, int]:
    async def _count(ct: str) -> tuple[str, int]:
        r = await db._execute(
            db.client.table("calls")
            .select("id", count=CountMethod.exact)
            .eq("call_type", ct)
            .limit(1)
        )
        return ct, int(r.count or 0)

    pairs = await asyncio.gather(*(_count(ct.value) for ct in CallType))
    return {ct: n for ct, n in pairs}


async def page_type_lifetime_counts(db: DB) -> dict[str, int]:
    async def _count(pt: str) -> tuple[str, int]:
        r = await db._execute(
            db.client.table("pages")
            .select("id", count=CountMethod.exact)
            .eq("page_type", pt)
            .limit(1)
        )
        return pt, int(r.count or 0)

    pairs = await asyncio.gather(*(_count(pt.value) for pt in PageType))
    return {pt: n for pt, n in pairs}


async def move_recent_counts(db: DB, *, scan: int = 500) -> dict[str, int]:
    """Count move executions in the recent ``scan`` calls' trace_json.

    Lifetime is intentionally not provided — exact lifetime counts would
    require walking every call's trace_json. The recent window is what
    we surface on the moves index.
    """
    res = await db._execute(
        db.client.table("calls").select("trace_json").order("created_at", desc=True).limit(scan)
    )
    rows = list(res.data or [])
    out: dict[str, int] = {}
    valid = {mt.value for mt in MoveType}
    for r in rows:
        events = r.get("trace_json") or []
        if not isinstance(events, list):
            continue
        for e in _iter_moves(events):
            mt = e.get("type") or e.get("move_type")
            if isinstance(mt, str) and mt in valid:
                out[mt] = out.get(mt, 0) + 1
    return out


def _iter_moves(events: Iterable[dict[str, Any]]) -> Iterable[dict[str, Any]]:
    for e in events:
        if not isinstance(e, dict):
            continue
        if e.get("event") != event_keys.MOVES_EXECUTED:
            continue
        for m in e.get("moves") or []:
            if isinstance(m, dict):
                yield m
