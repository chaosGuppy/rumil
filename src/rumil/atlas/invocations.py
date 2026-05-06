"""Recent example invocations for a CallType / DispatchDef / MoveDef.

Operators reading the catalog of moves/dispatches/calls want to see
"what does a real one look like" without leaving the page. This module
scans recent ``call_llm_exchanges`` rows and surfaces them in
Anthropic-API request/response shape.

For call types, every exchange of the matching call_type qualifies.
For dispatches and moves, an exchange qualifies when its ``tool_calls``
JSONB contains a ``tool_use`` block whose ``name`` matches the
dispatch / move's tool name (``DispatchDef.name`` or
``MoveDef.name``). The matching block is surfaced via
``InvocationRecord.match`` so the FE can highlight which tool_use the
operator was looking at.
"""

from __future__ import annotations

from typing import Any

from rumil.atlas.schemas import (
    InvocationIndex,
    InvocationMatch,
    InvocationRecord,
    InvocationRequest,
    InvocationResponse,
)
from rumil.calls.dispatches import (
    DISPATCH_DEFS,
    RECURSE_CLAIM_DISPATCH_DEF,
    RECURSE_DISPATCH_DEF,
)
from rumil.database import DB
from rumil.models import CallType, MoveType
from rumil.moves.registry import MOVES


def _request_from_exchange(row: dict[str, Any]) -> InvocationRequest:
    """Reconstruct the Anthropic-API-shape request body.

    Prefers ``request_kwargs`` (the full request that was actually
    sent) when present; falls back to assembling from ``system_prompt``
    + ``user_messages`` (or legacy ``user_message``).
    """
    rk = row.get("request_kwargs")
    if isinstance(rk, dict) and rk:
        sys_prompt = ""
        sys_field = rk.get("system")
        if isinstance(sys_field, str):
            sys_prompt = sys_field
        elif isinstance(sys_field, list):
            sys_prompt = "\n".join(
                str(b.get("text") or "") for b in sys_field if isinstance(b, dict)
            )
        return InvocationRequest(
            model=str(rk.get("model") or row.get("model") or ""),
            system=sys_prompt,
            messages=list(rk.get("messages") or []),
            tools=list(rk.get("tools") or []),
            temperature=rk.get("temperature"),
            max_tokens=rk.get("max_tokens"),
            thinking=rk.get("thinking") if isinstance(rk.get("thinking"), dict) else None,
        )

    user_messages = row.get("user_messages")
    if not isinstance(user_messages, list):
        user_msg = row.get("user_message") or ""
        if isinstance(user_msg, str) and user_msg:
            user_messages = [{"role": "user", "content": user_msg}]
        else:
            user_messages = []
    return InvocationRequest(
        model=str(row.get("model") or ""),
        system=str(row.get("system_prompt") or ""),
        messages=user_messages,
        tools=[],
        temperature=None,
        max_tokens=None,
        thinking=None,
    )


def _response_from_exchange(row: dict[str, Any]) -> InvocationResponse:
    """Reconstruct an Anthropic-API-shape response.

    The DB stores the response as separate columns (``response_text``,
    ``tool_calls`` list, ``thinking_blocks``, ``error``). Reassemble
    into a content-block list mirroring what the model emitted.
    """
    content: list[dict] = []
    thinking = row.get("thinking_blocks") or []
    if isinstance(thinking, list):
        for b in thinking:
            if isinstance(b, dict):
                content.append({"type": "thinking", **b})

    text = str(row.get("response_text") or "")
    if text:
        content.append({"type": "text", "text": text})

    tool_calls = row.get("tool_calls") or []
    if isinstance(tool_calls, list):
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            block = {"type": "tool_use"}
            for k in ("id", "name", "input"):
                if k in tc:
                    block[k] = tc[k]
            content.append(block)

    usage = None
    in_tok = row.get("input_tokens")
    out_tok = row.get("output_tokens")
    cache_creation = row.get("cache_creation_input_tokens")
    cache_read = row.get("cache_read_input_tokens")
    if any(v is not None for v in (in_tok, out_tok, cache_creation, cache_read)):
        usage = {
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "cache_creation_input_tokens": cache_creation,
            "cache_read_input_tokens": cache_read,
        }

    return InvocationResponse(
        content=content,
        stop_reason=None,
        usage=usage,
        error=row.get("error"),
        response_text=text,
        tool_calls=list(tool_calls) if isinstance(tool_calls, list) else [],
    )


def _match_tool_use(tool_calls: list[Any], tool_name: str) -> tuple[int, dict] | None:
    for i, tc in enumerate(tool_calls):
        if isinstance(tc, dict) and tc.get("name") == tool_name:
            return i, tc
    return None


async def _recent_exchanges(
    db: DB,
    *,
    call_type: str | None = None,
    project_id: str | None = None,
    scan: int = 200,
) -> list[dict[str, Any]]:
    """Most-recent exchanges, optionally filtered by call_type/project."""
    cols = (
        "id, call_id, run_id, phase, round, model, error, duration_ms, "
        "input_tokens, output_tokens, cache_creation_input_tokens, "
        "cache_read_input_tokens, created_at, system_prompt, user_message, "
        "user_messages, response_text, tool_calls, request_kwargs, "
        "thinking_blocks"
    )
    query = (
        db.client.table("call_llm_exchanges")
        .select(cols)
        .order("created_at", desc=True)
        .limit(scan)
    )
    res = await db._execute(query)
    raw = list(res.data or [])
    if not raw:
        return []

    if not (call_type or project_id):
        return raw

    call_ids = list({str(r.get("call_id")) for r in raw if r.get("call_id")})
    cres = await db._execute(
        db.client.table("calls")
        .select("id, call_type, run_id, project_id, status, cost_usd")
        .in_("id", call_ids)
    )
    meta = {str(c.get("id")): c for c in (cres.data or [])}
    out: list[dict[str, Any]] = []
    for r in raw:
        cid = str(r.get("call_id") or "")
        m = meta.get(cid) or {}
        if call_type and m.get("call_type") != call_type:
            continue
        if project_id and m.get("project_id") != project_id:
            continue
        r["_call_meta"] = m
        out.append(r)
    return out


async def _enrich_call_meta(db: DB, rows: list[dict[str, Any]]) -> None:
    """Attach call metadata (call_type, run_id, project_id, status, cost)
    to rows that don't already have it. Mutates ``rows``.
    """
    needed = [r for r in rows if "_call_meta" not in r]
    if not needed:
        return
    call_ids = list({str(r.get("call_id")) for r in needed if r.get("call_id")})
    if not call_ids:
        return
    cres = await db._execute(
        db.client.table("calls")
        .select("id, call_type, run_id, project_id, status, cost_usd")
        .in_("id", call_ids)
    )
    meta = {str(c.get("id")): c for c in (cres.data or [])}
    for r in needed:
        r["_call_meta"] = meta.get(str(r.get("call_id") or "")) or {}


def _record_from_exchange(
    row: dict[str, Any],
    *,
    match: InvocationMatch | None = None,
) -> InvocationRecord:
    meta = row.get("_call_meta") or {}
    return InvocationRecord(
        exchange_id=str(row.get("id") or ""),
        call_id=str(row.get("call_id") or ""),
        call_type=str(meta.get("call_type") or ""),
        run_id=str(row.get("run_id") or meta.get("run_id") or ""),
        project_id=str(meta.get("project_id") or ""),
        created_at=str(row.get("created_at") or ""),
        phase=str(row.get("phase") or ""),
        round=row.get("round"),
        cost_usd=meta.get("cost_usd"),
        duration_ms=row.get("duration_ms"),
        status=str(meta.get("status") or ""),
        has_error=bool(row.get("error")),
        request=_request_from_exchange(row),
        response=_response_from_exchange(row),
        match=match,
    )


async def build_call_exchanges(
    db: DB,
    call_id: str,
    *,
    limit: int = 50,
) -> InvocationIndex | None:
    """Every LLM exchange recorded against a single call.

    Distinct from ``build_call_type_invocations`` (which is keyed on
    the CallType across recent runs); this is keyed on a single
    call_id and is the natural drilldown when the run-flow page shows
    a node with ``n_llm_exchanges > 0``.
    """
    res = await db._execute(
        db.client.table("calls")
        .select("id, call_type, run_id, project_id, status, cost_usd")
        .eq("id", call_id)
        .limit(1)
    )
    crows = list(res.data or [])
    if not crows:
        return None
    call_meta = crows[0]

    cols = (
        "id, call_id, run_id, phase, round, model, error, duration_ms, "
        "input_tokens, output_tokens, cache_creation_input_tokens, "
        "cache_read_input_tokens, created_at, system_prompt, user_message, "
        "user_messages, response_text, tool_calls, request_kwargs, "
        "thinking_blocks"
    )
    res2 = await db._execute(
        db.client.table("call_llm_exchanges")
        .select(cols)
        .eq("call_id", call_id)
        .order("created_at")
        .limit(limit)
    )
    rows = list(res2.data or [])
    for r in rows:
        r["_call_meta"] = call_meta

    items = [_record_from_exchange(r) for r in rows]
    return InvocationIndex(
        kind="call",
        target=call_id,
        items=items,
        n_scanned=len(rows),
        truncated=False,
    )


async def build_call_type_invocations(
    db: DB,
    call_type: CallType,
    *,
    project_id: str | None = None,
    limit: int = 10,
    scan: int = 300,
) -> InvocationIndex:
    rows = await _recent_exchanges(db, call_type=call_type.value, project_id=project_id, scan=scan)
    items = [_record_from_exchange(r) for r in rows[:limit]]
    return InvocationIndex(
        kind="call_type",
        target=call_type.value,
        items=items,
        n_scanned=scan,
        truncated=len(rows) >= scan,
    )


async def _tool_keyed_invocations(
    db: DB,
    *,
    tool_name: str,
    kind: str,
    target: str,
    project_id: str | None = None,
    limit: int = 10,
    scan: int = 500,
) -> InvocationIndex:
    """Scan recent exchanges, filter to ones whose tool_calls contain
    ``tool_name``, surface those as invocations with the matched
    tool_use highlighted."""
    rows = await _recent_exchanges(db, project_id=project_id, scan=scan)
    await _enrich_call_meta(db, rows)
    items: list[InvocationRecord] = []
    for r in rows:
        tool_calls = r.get("tool_calls") or []
        if not isinstance(tool_calls, list):
            continue
        match = _match_tool_use(tool_calls, tool_name)
        if match is None:
            continue
        idx, tc = match
        items.append(
            _record_from_exchange(
                r,
                match=InvocationMatch(
                    tool_name=tool_name,
                    tool_input=dict(tc.get("input") or {}),
                    tool_use_id=str(tc.get("id") or "") or None,
                    block_index=idx,
                ),
            )
        )
        if len(items) >= limit:
            break
    return InvocationIndex(
        kind=kind,
        target=target,
        items=items,
        n_scanned=scan,
        truncated=len(rows) >= scan,
    )


async def build_dispatch_invocations(
    db: DB,
    call_type_or_label: str,
    *,
    project_id: str | None = None,
    limit: int = 10,
    scan: int = 500,
) -> InvocationIndex | None:
    """Recent invocations of one dispatch tool. Accepts a real CallType
    value (e.g. ``find_considerations``) or the literal labels
    ``recurse`` / ``recurse_claim``.
    """
    if call_type_or_label == "recurse":
        ddef = RECURSE_DISPATCH_DEF
    elif call_type_or_label == "recurse_claim":
        ddef = RECURSE_CLAIM_DISPATCH_DEF
    else:
        try:
            ct = CallType(call_type_or_label)
        except ValueError:
            return None
        ddef = DISPATCH_DEFS.get(ct)
        if ddef is None:
            return None
    return await _tool_keyed_invocations(
        db,
        tool_name=ddef.name,
        kind="dispatch",
        target=call_type_or_label,
        project_id=project_id,
        limit=limit,
        scan=scan,
    )


async def build_move_invocations(
    db: DB,
    move_type_value: str,
    *,
    project_id: str | None = None,
    limit: int = 10,
    scan: int = 500,
) -> InvocationIndex | None:
    try:
        mt = MoveType(move_type_value)
    except ValueError:
        return None
    move = MOVES.get(mt)
    if move is None:
        return None
    return await _tool_keyed_invocations(
        db,
        tool_name=move.name,
        kind="move",
        target=move_type_value,
        project_id=project_id,
        limit=limit,
        scan=scan,
    )
