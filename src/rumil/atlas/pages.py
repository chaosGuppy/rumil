"""Page-centric atlas surfaces.

Per-page-instance views (distinct from /atlas/registry/pages/{page_type},
which is the type-level taxonomy). Used to answer questions like:

- "Why is this question still open after 3 runs?"
- "Which calls touched this claim?"
- "When was this view first refreshed, and what came after?"
"""

from __future__ import annotations

from typing import Any

from rumil.atlas.schemas import (
    PageCallRef,
    PageInstanceCalls,
    PageTimeline,
    PageTimelineEvent,
)
from rumil.database import DB


def _call_to_ref(call_row: dict[str, Any], role: str) -> PageCallRef:
    return PageCallRef(
        call_id=str(call_row.get("id") or ""),
        call_type=str(call_row.get("call_type") or ""),
        run_id=str(call_row.get("run_id") or ""),
        role=role,
        created_at=str(call_row.get("created_at") or ""),
        cost_usd=float(call_row.get("cost_usd") or 0.0),
        status=str(call_row.get("status") or ""),
    )


async def _calls_with_in_context(db: DB, page_id: str) -> list[dict[str, Any]]:
    """Calls whose ``context_page_ids`` JSONB array contains ``page_id``.

    The column is JSONB (not a Postgres array), so the postgrest ``cs``
    filter wants a JSON-encoded value. ``.contains()`` doesn't escape
    that for us, so we drop down to ``.filter()`` and json.dumps the
    value ourselves.
    """
    import json

    res = await db._execute(
        db.client.table("calls")
        .select("id, call_type, run_id, status, cost_usd, created_at, context_page_ids")
        .filter("context_page_ids", "cs", json.dumps([page_id]))
        .order("created_at")
    )
    return list(res.data or [])


async def _calls_with_scope(db: DB, page_id: str) -> list[dict[str, Any]]:
    """Calls whose ``scope_page_id`` equals ``page_id``.

    Most orchestrator-driven calls (assess, find_considerations, scout_*,
    prioritization) target a question via ``scope_page_id`` rather than
    storing it in ``context_page_ids`` — so for any question-typed page,
    the scope match is usually the *primary* answer to "which calls
    touched this page". Without this, ``/pages/{id}/calls`` reports an
    empty ``in_context_of`` for every question targeted by orchestrator
    runs.
    """
    res = await db._execute(
        db.client.table("calls")
        .select("id, call_type, run_id, status, cost_usd, created_at, scope_page_id")
        .eq("scope_page_id", page_id)
        .order("created_at")
    )
    return list(res.data or [])


async def _calls_loaded_via_trace(db: DB, page_id: str) -> list[dict[str, Any]]:
    """Calls whose trace_json contains a load_page event for this page.

    Postgres jsonb @> isn't directly exposed by supabase-py's filter API
    in a portable way, so we run a coarse query (calls touching this
    page's scope or having it in context) and then verify in Python.
    A future RPC would do this server-side.
    """
    out: list[dict[str, Any]] = []
    res = await db._execute(
        db.client.table("calls")
        .select("id, call_type, run_id, status, cost_usd, created_at, trace_json")
        .order("created_at", desc=True)
        .limit(500)
    )
    for row in res.data or []:
        events = row.get("trace_json") or []
        if not isinstance(events, list):
            continue
        if any(
            isinstance(e, dict) and e.get("event") == "load_page" and e.get("page_id") == page_id
            for e in events
        ):
            out.append(row)
    return out


async def build_page_calls(db: DB, page_id: str) -> PageInstanceCalls | None:
    page = await db.get_page(page_id)
    if page is None:
        return None

    created_by: PageCallRef | None = None
    if page.provenance_call_id:
        res = await db._execute(
            db.client.table("calls")
            .select("id, call_type, run_id, status, cost_usd, created_at")
            .eq("id", page.provenance_call_id)
            .limit(1)
        )
        rows = list(res.data or [])
        if rows:
            created_by = _call_to_ref(rows[0], role="created")

    in_context_rows = await _calls_with_in_context(db, page_id)
    scope_rows = await _calls_with_scope(db, page_id)
    in_context_ids = {str(r.get("id")) for r in in_context_rows}
    in_context = [_call_to_ref(r, role="in_context") for r in in_context_rows]
    # Scope-only matches (calls that targeted the page via scope_page_id
    # without explicitly storing it in context_page_ids).
    scope_only = [
        _call_to_ref(r, role="scope") for r in scope_rows if str(r.get("id")) not in in_context_ids
    ]
    loaded = [_call_to_ref(r, role="loaded") for r in await _calls_loaded_via_trace(db, page_id)]

    superseded_by_page_id = getattr(page, "superseded_by", None) or None

    return PageInstanceCalls(
        page_id=page_id,
        page_type=page.page_type.value,
        headline=page.headline or "",
        created_by_call=created_by,
        in_context_of=[*scope_only, *in_context],
        loaded_by=loaded,
        superseded_by_page_id=superseded_by_page_id,
    )


async def build_page_timeline(db: DB, page_id: str) -> PageTimeline | None:
    page = await db.get_page(page_id)
    if page is None:
        return None

    events: list[PageTimelineEvent] = []
    events.append(
        PageTimelineEvent(
            ts=str(page.created_at),
            kind="created",
            call_id=page.provenance_call_id or None,
            call_type=page.provenance_call_type or None,
            run_id=page.run_id or None,
            detail=f"created as {page.page_type.value}",
        )
    )

    in_context_rows = await _calls_with_in_context(db, page_id)
    in_context_ids = {str(r.get("id")) for r in in_context_rows}
    for row in await _calls_with_scope(db, page_id):
        if str(row.get("id")) in in_context_ids:
            continue
        events.append(
            PageTimelineEvent(
                ts=str(row.get("created_at") or ""),
                kind="scoped",
                call_id=str(row.get("id") or ""),
                call_type=str(row.get("call_type") or ""),
                run_id=str(row.get("run_id") or ""),
                detail="page was the call's scope_page_id",
            )
        )

    for row in in_context_rows:
        events.append(
            PageTimelineEvent(
                ts=str(row.get("created_at") or ""),
                kind="in_context",
                call_id=str(row.get("id") or ""),
                call_type=str(row.get("call_type") or ""),
                run_id=str(row.get("run_id") or ""),
                detail="page was in this call's context",
            )
        )

    for row in await _calls_loaded_via_trace(db, page_id):
        events.append(
            PageTimelineEvent(
                ts=str(row.get("created_at") or ""),
                kind="loaded",
                call_id=str(row.get("id") or ""),
                call_type=str(row.get("call_type") or ""),
                run_id=str(row.get("run_id") or ""),
                detail="explicit load_page event",
            )
        )

    superseded_by = getattr(page, "superseded_by", None) or None
    if superseded_by:
        events.append(
            PageTimelineEvent(
                ts=str(page.created_at),
                kind="superseded",
                detail=f"superseded by {superseded_by[:8]}",
            )
        )

    events.sort(key=lambda e: e.ts)

    return PageTimeline(
        page_id=page_id,
        page_type=page.page_type.value,
        headline=page.headline or "",
        events=events,
    )
