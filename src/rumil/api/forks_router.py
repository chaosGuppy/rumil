"""Admin-only API for exchange forks: side-effect-free re-runs of a captured
LLM exchange with edited overrides.

All endpoints require admin. The router doesn't take a project_id query
param because forks are operator state, not workspace state — they reach
the base exchange via its UUID and don't filter by project.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from rumil.api.auth import AuthUser, _get_admin_db, require_admin
from rumil.database import DB
from rumil.forks import ForkOverrides, fire_fork, resolve_base


class BaseExchangeOut(BaseModel):
    """Reconstructed base exchange — what the fork editor pre-populates."""

    exchange_id: str
    call_id: str
    call_type: str | None
    system_prompt: str
    user_messages: list[dict]
    tools: list[dict]
    model: str
    temperature: float | None
    max_tokens: int
    has_thinking: bool
    thinking_off: bool


class ForkOut(BaseModel):
    id: str
    base_exchange_id: str
    overrides: dict
    overrides_hash: str
    sample_index: int
    model: str
    temperature: float | None
    response_text: str | None
    tool_calls: list[dict]
    stop_reason: str | None
    input_tokens: int | None
    output_tokens: int | None
    cache_creation_input_tokens: int | None
    cache_read_input_tokens: int | None
    duration_ms: int | None
    cost_usd: float | None
    error: str | None
    created_at: str | None
    created_by: str | None


class FireForkRequest(BaseModel):
    base_exchange_id: str
    overrides: ForkOverrides
    n_samples: int = 1


router = APIRouter(
    prefix="/api/exchange-forks",
    tags=["exchange-forks"],
    dependencies=[Depends(require_admin)],
)


@router.get("/base/{exchange_id}", response_model=BaseExchangeOut)
async def get_base(exchange_id: str, db: DB = Depends(_get_admin_db)) -> BaseExchangeOut:
    try:
        base = await resolve_base(db, exchange_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return BaseExchangeOut(
        exchange_id=base.exchange_id,
        call_id=base.call_id,
        call_type=base.call_type.value if base.call_type else None,
        system_prompt=base.system_prompt,
        user_messages=base.user_messages,
        tools=base.tools,
        model=base.model,
        temperature=base.temperature,
        max_tokens=base.max_tokens,
        has_thinking=base.has_thinking,
        thinking_off=base.thinking_off,
    )


@router.post("", response_model=list[ForkOut])
async def fire(
    body: FireForkRequest,
    user: AuthUser = Depends(require_admin),
    db: DB = Depends(_get_admin_db),
) -> list[ForkOut]:
    if body.n_samples < 1 or body.n_samples > 20:
        raise HTTPException(status_code=400, detail="n_samples must be in [1, 20]")
    try:
        rows = await fire_fork(
            db,
            body.base_exchange_id,
            body.overrides,
            body.n_samples,
            created_by=user.user_id or "anon",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return [
        ForkOut(
            id=r.id,
            base_exchange_id=r.base_exchange_id,
            overrides=r.overrides,
            overrides_hash=r.overrides_hash,
            sample_index=r.sample_index,
            model=r.model,
            temperature=r.temperature,
            response_text=r.response_text,
            tool_calls=r.tool_calls,
            stop_reason=r.stop_reason,
            input_tokens=r.input_tokens,
            output_tokens=r.output_tokens,
            cache_creation_input_tokens=r.cache_creation_input_tokens,
            cache_read_input_tokens=r.cache_read_input_tokens,
            duration_ms=r.duration_ms,
            cost_usd=r.cost_usd,
            error=r.error,
            created_at=r.created_at,
            created_by=r.created_by,
        )
        for r in rows
    ]


@router.get("", response_model=list[ForkOut])
async def list_forks(base_exchange_id: str, db: DB = Depends(_get_admin_db)) -> list[ForkOut]:
    rows = await db.list_forks_for_exchange(base_exchange_id)
    return [
        ForkOut(
            id=r["id"],
            base_exchange_id=r["base_exchange_id"],
            overrides=r["overrides"] or {},
            overrides_hash=r["overrides_hash"],
            sample_index=r["sample_index"],
            model=r["model"],
            temperature=r.get("temperature"),
            response_text=r.get("response_text"),
            tool_calls=r.get("tool_calls") or [],
            stop_reason=r.get("stop_reason"),
            input_tokens=r.get("input_tokens"),
            output_tokens=r.get("output_tokens"),
            cache_creation_input_tokens=r.get("cache_creation_input_tokens"),
            cache_read_input_tokens=r.get("cache_read_input_tokens"),
            duration_ms=r.get("duration_ms"),
            cost_usd=r.get("cost_usd"),
            error=r.get("error"),
            created_at=r.get("created_at"),
            created_by=r.get("created_by"),
        )
        for r in rows
    ]


@router.delete("/{fork_id}")
async def delete_fork(fork_id: str, db: DB = Depends(_get_admin_db)) -> dict[str, bool]:
    existing = await db.get_fork(fork_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Fork not found")
    await db.delete_fork(fork_id)
    return {"ok": True}
