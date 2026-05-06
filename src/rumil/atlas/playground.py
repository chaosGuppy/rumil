"""Playground integration: bundle an existing LLM exchange together
with its call's composition + any existing forks, so atlas pages can
render a fork-this-prompt playground inline.

The actual fork mechanics (firing a fork, listing them) live in
``rumil.forks`` / ``rumil.api.forks_router``. This module just bundles
what atlas needs into one response shape so the FE doesn't have to
chain three calls.
"""

from __future__ import annotations

from rumil.atlas.prompt_parts import build_prompt_composition
from rumil.atlas.render import _detect_anomalies
from rumil.atlas.schemas import ExchangePlaygroundContext, ForkSummary
from rumil.database import DB
from rumil.forks import resolve_base


async def build_playground_context(
    db: DB,
    exchange_id: str,
) -> ExchangePlaygroundContext | None:
    try:
        base = await resolve_base(db, exchange_id)
    except ValueError:
        return None

    call_type_value = base.call_type.value if base.call_type else ""

    # Pull the exchange's call row for run_id / project_id context.
    res = await db._execute(
        db.client.table("calls").select("run_id, project_id").eq("id", base.call_id).limit(1)
    )
    crows = list(res.data or [])
    run_id = str(crows[0].get("run_id") or "") if crows else ""
    project_id = str(crows[0].get("project_id") or "") if crows else ""

    # Composition for cross-link to the prompt parts the call type
    # claims to use.
    comp = build_prompt_composition(call_type_value) if call_type_value else None

    forks_rows = await db.list_forks_for_exchange(exchange_id)
    forks = [
        ForkSummary(
            id=str(r.get("id") or ""),
            overrides_hash=str(r.get("overrides_hash") or ""),
            sample_index=int(r.get("sample_index") or 0),
            model=str(r.get("model") or ""),
            temperature=r.get("temperature"),
            response_text=r.get("response_text"),
            has_error=bool(r.get("error")),
            cost_usd=r.get("cost_usd"),
            duration_ms=r.get("duration_ms"),
            created_at=r.get("created_at"),
            created_by=r.get("created_by"),
        )
        for r in forks_rows
    ]

    user_messages = list(base.user_messages or [])
    flat_user = ""
    for m in user_messages:
        if isinstance(m, dict):
            content = m.get("content") or ""
            if isinstance(content, str):
                flat_user += content + "\n\n"
            elif isinstance(content, list):
                for chunk in content:
                    if isinstance(chunk, dict) and chunk.get("type") == "text":
                        flat_user += str(chunk.get("text") or "") + "\n\n"
    flat_user = flat_user.strip()

    response_text = ""
    res2 = await db._execute(
        db.client.table("call_llm_exchanges")
        .select("response_text, created_at")
        .eq("id", exchange_id)
        .limit(1)
    )
    rows2 = list(res2.data or [])
    created_at = ""
    if rows2:
        response_text = str(rows2[0].get("response_text") or "")
        created_at = str(rows2[0].get("created_at") or "")

    return ExchangePlaygroundContext(
        exchange_id=exchange_id,
        call_id=base.call_id,
        call_type=call_type_value,
        run_id=run_id,
        project_id=project_id,
        created_at=created_at,
        model=base.model,
        temperature=base.temperature,
        max_tokens=base.max_tokens,
        has_thinking=base.has_thinking,
        thinking_off=base.thinking_off,
        system_prompt=base.system_prompt,
        user_messages=user_messages,
        user_message=flat_user,
        response_text=response_text,
        tools=list(base.tools or []),
        composition=comp,
        forks=forks,
        n_forks=len(forks),
        anomalies=_detect_anomalies(base.system_prompt, flat_user),
    )
