"""Rendered-prompt forensics — surface what the model actually saw.

Atlas's ``PromptComposition`` shows the *template* a call type uses;
``build_system_prompt`` substitutes ``{{TASK}}``, joins parts, and may
take different paths per caller. The composition view can't catch
runtime leaks: a call site passing ``task=None`` rendering the literal
``{{TASK}}``, parent-headline pollution from a recent assess into a
child context, or an ``ABSTRACT`` default silently inheriting where
``CONTENT`` was meant.

Two surfaces here:

- ``GET /atlas/registry/calls/{ct}/sample_render`` returns one recent
  real ``call_llm_exchanges`` row's ``system_prompt`` / ``user_message``
  with detected anomalies highlighted.
- ``GET /atlas/exchanges/search?q=`` runs a Postgres ILIKE over recent
  exchanges' rendered prompts. Coarse — but enough to catch
  ``{{TASK}}`` leaks and headline-pollution patterns.

Both endpoints scan a bounded recent window (most-recent N exchanges
across all calls). Costs scale with the cap, not the lifetime of the
DB.
"""

from __future__ import annotations

import re
from typing import Any

from rumil.atlas.schemas import (
    ExchangeSearchHit,
    ExchangeSearchResults,
    RenderedPromptSample,
)
from rumil.database import DB

_PLACEHOLDER_RE = re.compile(r"\{\{[A-Z_][A-Z0-9_]*\}\}")
_FALLBACK_TASK = "(see the user message for the specific task)"


def _detect_anomalies(system_prompt: str, user_message: str) -> list[str]:
    out: list[str] = []
    text = f"{system_prompt}\n{user_message}"
    leaks = _PLACEHOLDER_RE.findall(text)
    if leaks:
        unique = sorted(set(leaks))
        out.append(f"placeholder leak: {', '.join(unique)}")
    if _FALLBACK_TASK in system_prompt:
        out.append("task fallback stub used (caller passed task=None)")
    if system_prompt and "preamble" not in system_prompt.lower() and len(system_prompt) < 1000:
        out.append("system prompt unusually short — possible bypass of build_system_prompt")
    return out


async def _recent_exchanges(
    db: DB,
    *,
    call_type: str | None = None,
    project_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Most-recent exchanges, optionally filtered by call_type/project."""
    cols = (
        "id, call_id, phase, round, model, error, created_at, "
        "system_prompt, user_message, response_text"
    )
    query = (
        db.client.table("call_llm_exchanges")
        .select(cols)
        .order("created_at", desc=True)
        .limit(limit)
    )
    if call_type or project_id:
        # Inner join via call_id requires a separate query for the call_type.
        # Pull more exchanges than asked, filter in Python after a follow-up
        # call lookup. Cheap for ``limit <= 200``.
        raw_query = (
            db.client.table("call_llm_exchanges")
            .select(cols + ", call_id")
            .order("created_at", desc=True)
            .limit(max(limit * 5, 200))
        )
        res = await db._execute(raw_query)
        raw = list(res.data or [])
        if not raw:
            return []
        call_ids = list({str(r.get("call_id")) for r in raw if r.get("call_id")})
        cres = await db._execute(
            db.client.table("calls").select("id, call_type, run_id, project_id").in_("id", call_ids)
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
            r["call_type"] = m.get("call_type")
            r["run_id"] = m.get("run_id")
            out.append(r)
            if len(out) >= limit:
                break
        return out
    res = await db._execute(query)
    return list(res.data or [])


async def build_sample_render(
    db: DB,
    call_type: str,
    *,
    project_id: str | None = None,
    run_id: str | None = None,
) -> RenderedPromptSample | None:
    """Pick a recent exchange of ``call_type`` and surface its rendered
    prompt. When ``run_id`` is given, prefer exchanges from that run.
    """
    exchanges = await _recent_exchanges(db, call_type=call_type, project_id=project_id, limit=20)
    if run_id:
        prefer = [e for e in exchanges if e.get("run_id") == run_id]
        if prefer:
            exchanges = prefer
    if not exchanges:
        return None
    e = exchanges[0]
    sys_prompt = str(e.get("system_prompt") or "")
    usr = str(e.get("user_message") or "")
    return RenderedPromptSample(
        exchange_id=str(e.get("id") or ""),
        call_id=str(e.get("call_id") or ""),
        call_type=str(e.get("call_type") or call_type),
        run_id=str(e.get("run_id") or ""),
        created_at=str(e.get("created_at") or ""),
        model=str(e.get("model") or ""),
        phase=str(e.get("phase") or ""),
        round=e.get("round"),
        system_prompt=sys_prompt,
        user_message=usr,
        response_text=str(e.get("response_text") or ""),
        has_error=bool(e.get("error")),
        anomalies=_detect_anomalies(sys_prompt, usr),
    )


async def search_exchanges(
    db: DB,
    query: str,
    *,
    project_id: str | None = None,
    call_type: str | None = None,
    limit: int = 50,
    scan: int = 500,
) -> ExchangeSearchResults:
    """Scan the most recent ``scan`` exchanges and return matches.

    Substring search across ``system_prompt`` / ``user_message`` /
    ``response_text``. Coarse but catches ``{{TASK}}`` leaks and
    headline-pollution patterns the static composition view can't see.
    """
    q = (query or "").strip()
    if not q:
        return ExchangeSearchResults(
            query="",
            hits=[],
            total=0,
            truncated=False,
            n_scanned=0,
        )
    raw_query = (
        db.client.table("call_llm_exchanges")
        .select("id, call_id, error, created_at, system_prompt, user_message, response_text")
        .order("created_at", desc=True)
        .limit(scan)
    )
    res = await db._execute(raw_query)
    raw = list(res.data or [])
    if not raw:
        return ExchangeSearchResults(query=q, hits=[], total=0, truncated=False, n_scanned=0)

    call_ids = list({str(r.get("call_id")) for r in raw if r.get("call_id")})
    cmeta: dict[str, dict[str, Any]] = {}
    if call_ids:
        cres = await db._execute(
            db.client.table("calls").select("id, call_type, run_id, project_id").in_("id", call_ids)
        )
        cmeta = {str(c.get("id")): c for c in (cres.data or [])}

    q_lower = q.lower()
    hits: list[ExchangeSearchHit] = []
    for r in raw:
        cid = str(r.get("call_id") or "")
        m = cmeta.get(cid) or {}
        if call_type and m.get("call_type") != call_type:
            continue
        if project_id and m.get("project_id") != project_id:
            continue
        for field in ("system_prompt", "user_message", "response_text"):
            text = str(r.get(field) or "")
            idx = text.lower().find(q_lower)
            if idx == -1:
                continue
            start = max(0, idx - 80)
            end = min(len(text), idx + 80 + len(q))
            prefix = "…" if start > 0 else ""
            suffix = "…" if end < len(text) else ""
            snippet = f"{prefix}{text[start:end].strip()}{suffix}"
            hits.append(
                ExchangeSearchHit(
                    exchange_id=str(r.get("id") or ""),
                    call_id=cid,
                    call_type=str(m.get("call_type") or ""),
                    run_id=str(m.get("run_id") or ""),
                    created_at=str(r.get("created_at") or ""),
                    field=field,
                    snippet=snippet,
                )
            )
            break
        if len(hits) >= limit:
            break
    return ExchangeSearchResults(
        query=q,
        hits=hits,
        total=len(hits),
        truncated=len(raw) >= scan,
        n_scanned=len(raw),
    )
