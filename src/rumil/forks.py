"""Exchange forks: side-effect-free re-runs of a captured LLM exchange
with edited overrides.

Forks are admin-only operator state. They never write to pages, links, or
mutation_events; they don't participate in the staged-runs visibility model;
no trace events are recorded. The base exchange is the canonical starting
point — overrides replace specific fields, samples are fired in parallel,
and rows persist to ``exchange_forks`` for side-by-side viewing.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, replace

import anthropic
from anthropic.types import (
    ServerToolUseBlock,
    TextBlock,
    ToolUseBlock,
    WebSearchToolResultBlock,
)
from langfuse import observe
from pydantic import BaseModel
from tenacity import retry, retry_if_exception, wait_exponential

from rumil.available_moves import get_moves_for_call
from rumil.database import DB
from rumil.llm import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_TEMPERATURE,
    _is_retryable,
    _langfuse_input_for,
    _log_before_retry,
    _stop_after_status_retries,
    _supports_sampling_params,
    derive_model_config,
    thinking_config,
)
from rumil.model_config import ModelConfig, model_config_from_record
from rumil.models import CallType
from rumil.moves.registry import MOVES
from rumil.pricing import compute_cost
from rumil.settings import get_settings
from rumil.tracing.langfuse_client import get_langfuse, phase_span

log = logging.getLogger(__name__)


_fork_retry = retry(
    retry=retry_if_exception(_is_retryable),
    stop=_stop_after_status_retries,
    wait=wait_exponential(multiplier=1, min=1, max=60),
    before_sleep=_log_before_retry,
    reraise=True,
)


class ForkOverrides(BaseModel):
    """Partial overrides for a base exchange.

    Fields left as ``None`` inherit from the base. ``tools`` is a full
    replacement — to remove a tool, omit it from the override; to add or
    edit one, include the desired full Anthropic tool dict
    (``{"name", "description", "input_schema"}``).

    ``thinking_off`` toggles adaptive thinking off for models that have it
    enabled by default (Opus 4.7/4.6, Sonnet 4.6). Leaving as ``None``
    inherits the model's default behavior; ``True`` disables thinking even
    on models where it's normally always-on.
    """

    system_prompt: str | None = None
    user_messages: list[dict] | None = None
    tools: list[dict] | None = None
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    thinking_off: bool | None = None


@dataclass
class BaseExchange:
    """Reconstructed base exchange — what the original API call sent.

    ``original_config`` holds the typed ``ModelConfig`` that was
    actually applied on the wire (sampling, thinking, effort), captured
    at exchange-write time. When non-None, fork rebuilds prefer it over
    re-deriving from current rules — so a fork reproduces the original
    condition even if rules changed since. Cleared when the user
    overrides the model (a captured config is tied to its model).

    The flat ``temperature`` / ``max_tokens`` / ``has_thinking`` /
    ``thinking_off`` fields stay on this dataclass for back-compat with
    the existing fork UI / overrides flow, but the source of truth at
    the point of replay is ``original_config`` when present.
    """

    exchange_id: str
    call_id: str
    call_type: CallType | None
    system_prompt: str
    user_messages: list[dict]
    tools: list[dict]
    model: str
    temperature: float | None
    max_tokens: int
    has_thinking: bool
    thinking_off: bool
    original_config: ModelConfig | None = None


@dataclass
class ForkRow:
    """A single persisted fork sample row."""

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
    created_at: str | None = None
    created_by: str | None = None


def hash_overrides(overrides: dict) -> str:
    """Stable 16-char hash of normalized overrides JSON. Drops null values."""
    cleaned = {k: v for k, v in overrides.items() if v is not None}
    payload = json.dumps(cleaned, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def move_type_to_tool_dict(move_type) -> dict:
    """Build an Anthropic tool dict for a MoveType from the moves registry."""
    move_def = MOVES[move_type]
    return {
        "name": move_def.name,
        "description": move_def.description,
        "input_schema": move_def.schema.model_json_schema(),
    }


async def resolve_base(db: DB, base_exchange_id: str) -> BaseExchange:
    """Load the base exchange row and reconstruct its API inputs.

    The exchange row carries the verbatim system prompt and user message(s).
    The tool list is rebuilt from the parent call's call_type via the active
    available_moves preset — exchange rows don't store tools directly, so
    this is a best-effort reconstruction.

    The model is read from the exchange row's ``model`` column (added in
    migration 20260501000019). For pre-migration rows where it's NULL, fall
    back to the run config — versus runs put it at
    ``runs.config['judge_config']['model']``, normal runs at
    ``runs.config['model']`` (captured by ``Settings.capture_config()``).
    Last resort is ``settings.model``.
    """
    row = await db.get_llm_exchange(base_exchange_id)
    if row is None:
        raise ValueError(f"No exchange found with id {base_exchange_id}")

    user_messages = row.get("user_messages")
    if not user_messages:
        single = row.get("user_message")
        user_messages = [{"role": "user", "content": single}] if single else []

    call_type: CallType | None = None
    tools: list[dict] = []
    if row.get("call_id"):
        call = await db.get_call(row["call_id"])
        if call is not None:
            call_type = call.call_type
            try:
                move_types = get_moves_for_call(call_type)
                tools = [move_type_to_tool_dict(mt) for mt in move_types]
            except (ValueError, KeyError) as exc:
                log.warning(
                    "Could not reconstruct tools for call_type=%s: %s",
                    call_type,
                    exc,
                )

    settings = get_settings()
    model = row.get("model")
    if not model and row.get("run_id"):
        run = await db.get_run(row["run_id"])
        cfg = (run or {}).get("config") or {}
        judge_cfg = cfg.get("judge_config") if isinstance(cfg, dict) else None
        if isinstance(judge_cfg, dict) and judge_cfg.get("model"):
            model = judge_cfg["model"]
        elif isinstance(cfg, dict) and cfg.get("model"):
            model = cfg["model"]
    if not model:
        model = settings.model
    raw_kwargs = row.get("request_kwargs") if isinstance(row.get("request_kwargs"), dict) else None
    captured = model_config_from_record(raw_kwargs) if raw_kwargs is not None else None
    has_thinking = (
        captured.thinking is not None
        if captured is not None
        else thinking_config(model) is not None
    )
    return BaseExchange(
        exchange_id=base_exchange_id,
        call_id=row.get("call_id", ""),
        call_type=call_type,
        system_prompt=row.get("system_prompt") or "",
        user_messages=list(user_messages),
        tools=tools,
        model=model,
        temperature=captured.temperature
        if captured is not None
        else (DEFAULT_TEMPERATURE if _supports_sampling_params(model) else None),
        max_tokens=captured.max_tokens if captured is not None else DEFAULT_MAX_TOKENS,
        has_thinking=has_thinking,
        thinking_off=False,
        original_config=captured,
    )


def merge_overrides(base: BaseExchange, overrides: ForkOverrides) -> BaseExchange:
    """Apply non-null override fields onto the base.

    A model override drops ``original_config`` since the captured thinking /
    effort were tied to the original model and don't transfer cleanly. Same
    model: the captured config survives so build_kwargs can reproduce it.
    """
    model_overridden = overrides.model is not None and overrides.model != base.model
    merged_model = overrides.model if overrides.model is not None else base.model
    captured = None if model_overridden else base.original_config
    has_thinking = (
        captured.thinking is not None
        if captured is not None
        else thinking_config(merged_model) is not None
    )
    return BaseExchange(
        exchange_id=base.exchange_id,
        call_id=base.call_id,
        call_type=base.call_type,
        system_prompt=overrides.system_prompt
        if overrides.system_prompt is not None
        else base.system_prompt,
        user_messages=overrides.user_messages
        if overrides.user_messages is not None
        else base.user_messages,
        tools=overrides.tools if overrides.tools is not None else base.tools,
        model=merged_model,
        temperature=overrides.temperature
        if overrides.temperature is not None
        else base.temperature,
        max_tokens=overrides.max_tokens if overrides.max_tokens is not None else base.max_tokens,
        has_thinking=has_thinking,
        thinking_off=overrides.thinking_off
        if overrides.thinking_off is not None
        else base.thinking_off,
        original_config=captured,
    )


def build_kwargs(merged: BaseExchange) -> dict:
    """Build Anthropic messages.create kwargs from a merged base+overrides.

    When ``original_config`` is present (captured at exchange-write time),
    its ``to_anthropic_kwargs()`` is the source of truth — thinking and
    output_config track the original even if the underlying rules changed
    since. ``thinking_off=True`` still wins as an explicit user override.
    Without a captured config we fall back to ``derive_model_config(model)``,
    matching the pre-capture behavior.
    """
    kwargs: dict = {
        "model": merged.model,
        "system": merged.system_prompt,
        "messages": merged.user_messages,
    }
    captured = merged.original_config
    if captured is not None:
        # Override the temperature from BaseExchange when one was set explicitly,
        # otherwise let the captured config carry it.
        cfg = captured
        if merged.thinking_off:
            cfg = replace(cfg, thinking=None)
        kwargs.update(cfg.to_anthropic_kwargs())
        # An explicit fork-time temperature override (different from captured)
        # still wins, mirroring the pre-capture override behavior.
        if (
            _supports_sampling_params(merged.model)
            and merged.temperature is not None
            and merged.temperature != cfg.temperature
        ):
            kwargs["temperature"] = merged.temperature
        if merged.max_tokens != cfg.max_tokens:
            kwargs["max_tokens"] = merged.max_tokens
    else:
        cfg = derive_model_config(merged.model, max_tokens=merged.max_tokens)
        if merged.thinking_off:
            cfg = replace(cfg, thinking=None)
        # Honor an explicit override-time temperature even on the recompute path.
        if merged.temperature is not None and _supports_sampling_params(merged.model):
            cfg = replace(cfg, temperature=merged.temperature)
        elif not _supports_sampling_params(merged.model):
            cfg = replace(cfg, temperature=None)
        kwargs.update(cfg.to_anthropic_kwargs())
    if merged.tools:
        kwargs["tools"] = merged.tools
    return kwargs


def _extract_response(response: anthropic.types.Message) -> tuple[str | None, list[dict]]:
    """Pull text and tool_use blocks out of a response message."""
    text_parts: list[str] = []
    tool_calls: list[dict] = []
    for block in response.content:
        if isinstance(block, TextBlock):
            text_parts.append(block.text)
        elif isinstance(block, (ToolUseBlock, ServerToolUseBlock)):
            tool_calls.append({"name": block.name, "input": block.input})
        elif isinstance(block, WebSearchToolResultBlock):
            tool_calls.append(
                {
                    "type": "web_search_tool_result",
                    "tool_use_id": block.tool_use_id,
                    "content": block.model_dump(mode="json")["content"],
                }
            )
    return ("\n".join(text_parts) or None), tool_calls


@_fork_retry
@observe(as_type="generation", name="anthropic.messages.stream.fork")
async def _do_call(
    client: anthropic.AsyncAnthropic, kwargs: dict, model: str
) -> tuple[anthropic.types.Message, int]:
    start = time.monotonic()
    async with client.messages.stream(**kwargs) as stream:
        try:
            response = await stream.get_final_message()
        except Exception:
            # Pull whatever was decoded before the stream raised and
            # attach it to the active langfuse generation as ``output``.
            # ``@observe`` will layer ``level=ERROR`` + ``status_message``
            # on re-raise; we just need to surface the partial here so
            # admins can see what the model produced before collapse.
            partial_text: str | None = None
            try:
                content = stream.current_message_snapshot.content
                partial_text = "".join(b.text for b in content if isinstance(b, TextBlock)) or None
            except Exception:
                pass
            lf = get_langfuse()
            if lf is not None:
                try:
                    params = {
                        k: kwargs.get(k)
                        for k in ("temperature", "top_p", "max_tokens", "thinking")
                        if kwargs.get(k) is not None
                    }
                    lf.update_current_generation(
                        model=model,
                        input=_langfuse_input_for(
                            kwargs.get("system") or "", kwargs.get("messages") or []
                        ),
                        output=partial_text,
                        model_parameters=params or None,
                    )
                except Exception as lf_exc:
                    log.debug("Langfuse partial-failure enrichment (fork) failed: %s", lf_exc)
            raise
    elapsed_ms = int((time.monotonic() - start) * 1000)
    _enrich_fork_generation(model=model, kwargs=kwargs, response=response, elapsed_ms=elapsed_ms)
    return response, elapsed_ms


def _enrich_fork_generation(
    *,
    model: str,
    kwargs: dict,
    response: anthropic.types.Message,
    elapsed_ms: int,
) -> None:
    """Populate the active Langfuse generation span. No-op when disabled."""
    lf = get_langfuse()
    if lf is None:
        return
    try:
        usage = response.usage
        cache_creation = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cost_usd = compute_cost(
            model=model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_creation_input_tokens=cache_creation,
            cache_read_input_tokens=cache_read,
        )
        output_text = "".join(b.text for b in response.content if isinstance(b, TextBlock))
        params = {
            k: kwargs.get(k)
            for k in ("temperature", "top_p", "max_tokens", "thinking")
            if kwargs.get(k) is not None
        }
        lf.update_current_generation(
            model=model,
            input=_langfuse_input_for(kwargs.get("system") or "", kwargs.get("messages") or []),
            output=output_text or None,
            model_parameters=params or None,
            usage_details={
                "input": usage.input_tokens,
                "output": usage.output_tokens,
                "cache_creation_input": cache_creation,
                "cache_read_input": cache_read,
            },
            cost_details={"total": cost_usd} if cost_usd else None,
            metadata={"stop_reason": response.stop_reason, "duration_ms": elapsed_ms},
        )
    except Exception as exc:
        log.debug("Langfuse enrichment failed (fork): %s", exc)


async def _fire_one_sample(client: anthropic.AsyncAnthropic, kwargs: dict, model: str) -> dict:
    """Fire a single sample. Returns a partial row dict (no id/index yet)."""
    try:
        response, elapsed_ms = await _do_call(client, kwargs, model)
    except Exception as exc:
        log.error("Fork sample failed: %s", exc, exc_info=True)
        return {
            "model": model,
            "error": f"{type(exc).__name__}: {exc}",
            "duration_ms": None,
            "response_text": None,
            "tool_calls": [],
            "stop_reason": None,
            "input_tokens": None,
            "output_tokens": None,
            "cache_creation_input_tokens": None,
            "cache_read_input_tokens": None,
            "cost_usd": None,
        }
    response_text, tool_calls = _extract_response(response)
    cache_creation = getattr(response.usage, "cache_creation_input_tokens", 0) or 0
    cache_read = getattr(response.usage, "cache_read_input_tokens", 0) or 0
    cost_usd = compute_cost(
        model=model,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        cache_creation_input_tokens=cache_creation,
        cache_read_input_tokens=cache_read,
    )
    return {
        "model": model,
        "error": None,
        "duration_ms": elapsed_ms,
        "response_text": response_text,
        "tool_calls": tool_calls,
        "stop_reason": response.stop_reason,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "cache_creation_input_tokens": cache_creation or None,
        "cache_read_input_tokens": cache_read or None,
        "cost_usd": cost_usd or None,
    }


async def fire_fork(
    db: DB,
    base_exchange_id: str,
    overrides: ForkOverrides,
    n_samples: int,
    *,
    created_by: str | None = None,
) -> list[ForkRow]:
    """Fire ``n_samples`` parallel forks of a base exchange.

    Tools provided to the API are *not executed* — any ``tool_use`` blocks
    in the response are returned as data only. No trace events, no workspace
    mutation, no participation in staged runs.
    """
    if n_samples < 1:
        raise ValueError(f"n_samples must be >= 1, got {n_samples}")

    base = await resolve_base(db, base_exchange_id)
    merged = merge_overrides(base, overrides)
    kwargs = build_kwargs(merged)

    overrides_dict = overrides.model_dump(exclude_none=True)
    overrides_hash = hash_overrides(overrides_dict)

    settings = get_settings()
    client = anthropic.AsyncAnthropic(api_key=settings.require_anthropic_key())

    with phase_span(f"fork.{base_exchange_id[:8]}"):
        results = await asyncio.gather(
            *(_fire_one_sample(client, kwargs, merged.model) for _ in range(n_samples)),
            return_exceptions=False,
        )

    rows: list[ForkRow] = []
    for result in results:
        row = await db.save_fork(
            base_exchange_id=base_exchange_id,
            overrides=overrides_dict,
            overrides_hash=overrides_hash,
            model=result["model"],
            temperature=merged.temperature,
            response_text=result["response_text"],
            tool_calls=result["tool_calls"],
            stop_reason=result["stop_reason"],
            input_tokens=result["input_tokens"],
            output_tokens=result["output_tokens"],
            cache_creation_input_tokens=result["cache_creation_input_tokens"],
            cache_read_input_tokens=result["cache_read_input_tokens"],
            duration_ms=result["duration_ms"],
            cost_usd=result["cost_usd"],
            error=result["error"],
            created_by=created_by,
        )
        rows.append(row)
    return rows
