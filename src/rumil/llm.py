"""
LLM interface. Wraps the Anthropic API and handles exchange persistence.

Exports:
  - call_api: API call with retries and optional exchange logging
  - text_call: plain text call
  - structured_call: structured output via messages.parse

Data types: Tool, ToolCall, RoundRecord, AgentResult, APIResponse,
            LLMExchangeMetadata.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, Generic, TypeVar, overload

import anthropic
from anthropic.types import (
    ServerToolUseBlock,
    TextBlock,
    ToolUseBlock,
    WebSearchToolResultBlock,
)
from pydantic import BaseModel, ValidationError
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception,
    wait_exponential,
)

from rumil.pricing import compute_cost
from rumil.settings import get_settings
from rumil.tracing.trace_events import ErrorEvent, LLMExchangeEvent
from rumil.tracing.tracer import get_trace

if TYPE_CHECKING:
    from rumil.database import DB

DEFAULT_MAX_TOKENS = 20_000
DEFAULT_TEMPERATURE = 0.15


def _supports_sampling_params(model: str) -> bool:
    # Opus 4.7 removed temperature/top_p/top_k — sending any returns 400.
    # With adaptive thinking on (Opus 4.6, Sonnet 4.6), temperature must be
    # 1.0 — we'd rather skip it than set 1.0, so gate on thinking being off.
    if model.startswith("claude-opus-4-7"):
        return False
    return _thinking_config(model) is None


def _thinking_config(model: str) -> dict | None:
    # Adaptive thinking: Opus 4.7/4.6 and Sonnet 4.6. Haiku and older Sonnet
    # don't support adaptive. On 4.7, thinking text is omitted by default —
    # ask for summarized so sdk_agent can still capture it.
    if model.startswith("claude-opus-4-7"):
        return {"type": "adaptive", "display": "summarized"}
    if model.startswith(("claude-opus-4-6", "claude-sonnet-4-6")):
        return {"type": "adaptive"}
    return None


def _effort_level(model: str) -> str | None:
    # xhigh is Opus 4.7-only; high is the best shared setting elsewhere.
    # Haiku and Sonnet 4.5 don't support the effort parameter at all.
    if model.startswith("claude-opus-4-7"):
        return "xhigh"
    if model.startswith(("claude-opus-4-6", "claude-sonnet-4-6")):
        return "high"
    return None


PROMPTS_DIR = Path(__file__).parent.parent.parent / "prompts"

log = logging.getLogger(__name__)


def _is_retryable(exc: BaseException) -> bool:
    """Return True if the exception represents a transient API error."""
    status = getattr(exc, "status_code", None)
    name = type(exc).__name__.lower()
    return (
        status in (429, 500, 529)
        or "overloaded" in name
        or "ratelimit" in name
        or "internalserver" in name
        or "overloaded" in str(exc).lower()
    )


def _stop_after_status_retries(retry_state: RetryCallState) -> bool:
    """Stop callback that respects per-status retry limits from settings."""
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    status = getattr(exc, "status_code", None) if exc else None
    max_retries = get_settings().get_max_retries(status)
    return retry_state.attempt_number >= max_retries


def _log_before_retry(retry_state: RetryCallState) -> None:
    """Log a warning before each retry attempt."""
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    status = getattr(exc, "status_code", None) if exc else None
    name = type(exc).__name__.lower() if exc else "unknown"
    label = f"HTTP {status}" if status else name
    wait = retry_state.next_action.sleep if retry_state.next_action else 0
    max_retries = get_settings().get_max_retries(status)
    log.warning(
        "API temporarily unavailable (%s), retrying in %gs (attempt %d/%d)",
        label,
        wait,
        retry_state.attempt_number,
        max_retries,
    )
    print(
        f"  [retry] API {label}, waiting {wait:g}s "
        f"(attempt {retry_state.attempt_number}/{max_retries})",
        file=sys.stderr,
        flush=True,
    )


_api_retry = retry(
    retry=retry_if_exception(_is_retryable),
    stop=_stop_after_status_retries,
    wait=wait_exponential(multiplier=1, min=1, max=60),
    before_sleep=_log_before_retry,
    reraise=True,
)


def _load_file(name: str) -> str:
    path = PROMPTS_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {path}")
    return path.read_text(encoding="utf-8")


_experimental_scout_budget: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "experimental_scout_budget", default=None
)


def set_experimental_scout_budget(budget: int | None) -> contextvars.Token:
    """Set the prioritiser's budget visible to scouts on the experimental path.

    Returns a token that can be passed to `reset_experimental_scout_budget`.
    """
    return _experimental_scout_budget.set(budget)


def reset_experimental_scout_budget(token: contextvars.Token) -> None:
    _experimental_scout_budget.reset(token)


_SCOUT_BUDGET_CALL_TYPES: frozenset[str] = frozenset(
    {
        "scout_subquestions",
        "scout_estimates",
        "scout_hypotheses",
        "scout_analogies",
        "scout_paradigm_cases",
        "scout_factchecks",
        "scout_web_questions",
        "scout_deep_questions",
    }
)


def build_system_prompt(
    call_type: str,
    *,
    include_preamble: bool = True,
    include_citations: bool = True,
) -> str:
    """Combine preamble + call-type instructions + citations into one system prompt.

    Pass ``include_citations=False`` for calls that do not create any content-bearing
    pages (e.g. prioritization, scoring) — the inline-citation rules have nothing to
    attach to in those calls and only add noise.

    Pass ``include_preamble=False`` for calls whose prompts must not assume any
    rumil-workspace framing (e.g. generate_artefact, where the LLM is acting as
    a domain-neutral writer with only a spec for context). When preamble is off,
    citations and grounding are also skipped since they're workspace-specific.
    """
    instructions = _load_file(f"{call_type}.md")
    if not include_preamble:
        return instructions
    preamble = _load_file("preamble.md")
    grounding = _load_file("grounding.md")
    parts = [preamble, instructions]
    if include_citations:
        parts.append(_load_file("citations.md"))
    parts.append(grounding)
    budget = _experimental_scout_budget.get()
    if budget is not None and call_type in _SCOUT_BUDGET_CALL_TYPES:
        budget_awareness = _load_file("scout_budget_awareness_experimental.md").format(
            budget=budget
        )
        parts.append(budget_awareness)
    return "\n\n---\n\n".join(parts)


def build_user_message(context_text: str, task_description: str) -> str:
    """Combine context dump + specific task into one user message."""
    if context_text:
        return f"{context_text}\n\n---\n\n{task_description}"
    return task_description


_CACHE_BREAKPOINT = {"type": "ephemeral"}


def _add_cache_breakpoint(messages: Sequence[dict]) -> list[dict]:
    """Return a shallow copy of messages with a cache breakpoint on the last block.

    Mutates nothing — copies only the last message and its content.
    """
    if not messages:
        return list(messages)
    msgs = list(messages)
    last = dict(msgs[-1])
    content = last.get("content")
    if isinstance(content, str):
        last["content"] = [
            {"type": "text", "text": content, "cache_control": _CACHE_BREAKPOINT},
        ]
    elif isinstance(content, list) and content:
        content = list(content)
        last_block = content[-1]
        if isinstance(last_block, dict):
            content[-1] = {**last_block, "cache_control": _CACHE_BREAKPOINT}
        else:
            content[-1] = {
                **last_block.model_dump(),
                "cache_control": _CACHE_BREAKPOINT,
            }
        last["content"] = content
    msgs[-1] = last
    return msgs


def _with_date_suffix(system_prompt: str) -> str:
    """Append today's date to the system prompt."""
    today = date.today().strftime("%Y-%m-%d")
    return system_prompt + f"\n\nIMPORTANT: Today's date is {today}\n"


_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)


def _extract_json(text: str) -> dict:
    """Extract a JSON object from LLM response text.

    Tries fenced code blocks first, then bare JSON.
    """
    m = _JSON_BLOCK_RE.search(text)
    if m:
        return json.loads(m.group(1))
    stripped = text.strip()
    start = stripped.find("{")
    if start != -1:
        return json.loads(stripped[start : stripped.rfind("}") + 1])
    raise ValueError(f"No JSON found in response: {text[:200]}")


def _serialize_messages(messages: Sequence[dict]) -> list[dict]:
    """Serialize messages for JSON storage, converting SDK objects to dicts."""
    result = []
    for msg in messages:
        out: dict = {"role": msg["role"]}
        content = msg.get("content")
        if isinstance(content, str):
            out["content"] = content
        elif isinstance(content, list):
            blocks = []
            for block in content:
                if isinstance(block, dict):
                    blocks.append(block)
                elif hasattr(block, "model_dump"):
                    blocks.append(block.model_dump())
                else:
                    blocks.append(str(block))
            out["content"] = blocks
        else:
            out["content"] = str(content) if content is not None else None
        result.append(out)
    return result


def _schema_instruction(response_model: type[BaseModel]) -> str:
    """Build a JSON schema instruction block for the model."""
    schema = response_model.model_json_schema()
    return (
        "\n\nRespond with ONLY a JSON object matching this schema "
        "(no other text, no markdown fences):\n" + json.dumps(schema, indent=2)
    )


@dataclass
class Tool:
    """A tool available to the LLM. fn is called with the parsed input dict
    and returns a string result that is sent back as tool_result."""

    name: str
    description: str
    input_schema: dict
    fn: Callable[[dict], Awaitable[str]]


@dataclass
class ToolCall:
    """Record of a single tool call made during an agent loop."""

    name: str
    input: dict
    result: str


@dataclass
class APIResponse:
    """Wrapper around an Anthropic Message with timing info."""

    message: anthropic.types.Message
    duration_ms: int


@dataclass
class RoundRecord:
    """Record of a single API round within an agent loop."""

    round: int
    response_text: str
    tool_calls: list[ToolCall]
    input_tokens: int
    output_tokens: int
    duration_ms: int = 0
    error: str | None = None


@dataclass
class AgentResult:
    """Result of an agent loop run."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    rounds: list[RoundRecord] = field(default_factory=list)
    system_prompt: str = ""
    user_message: str = ""
    warnings: list[str] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)


@dataclass
class LLMExchangeMetadata:
    """Context for automatically saving an LLM exchange to the database.

    Encapsulates the parameters needed by save_llm_exchange that are not
    already present in the call_api / structured_call signatures.

    `user_message` is only used for single-turn calls (text_call). For
    multi-turn calls the full message stack is serialized automatically
    from the `messages` arg and persisted to `user_messages` on the
    exchange row.
    """

    call_id: str
    phase: str
    round_num: int | None = None
    user_message: str | None = None


async def _save_exchange(
    metadata: LLMExchangeMetadata,
    db: DB,
    model: str,
    system_prompt: str,
    response_text: str | None,
    tool_calls: list[dict],
    input_tokens: int,
    output_tokens: int,
    duration_ms: int,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
    user_messages: Sequence[dict] | None = None,
) -> None:
    """Persist an LLM exchange and record a trace event."""
    exchange_id = await db.save_llm_exchange(
        call_id=metadata.call_id,
        phase=metadata.phase,
        system_prompt=system_prompt,
        user_message=metadata.user_message,
        response_text=response_text,
        tool_calls=tool_calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        duration_ms=duration_ms,
        round_num=metadata.round_num,
        cache_creation_input_tokens=cache_creation_input_tokens or None,
        cache_read_input_tokens=cache_read_input_tokens or None,
        user_messages=user_messages,
    )
    cost_usd = compute_cost(
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
    )
    trace = get_trace()
    if trace:
        await trace.record(
            LLMExchangeEvent(
                exchange_id=exchange_id,
                phase=metadata.phase,
                round=metadata.round_num,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_creation_input_tokens=cache_creation_input_tokens or None,
                cache_read_input_tokens=cache_read_input_tokens or None,
                duration_ms=duration_ms,
                cost_usd=cost_usd or None,
            )
        )


async def call_api(
    client: anthropic.AsyncAnthropic,
    model: str,
    system_prompt: str,
    messages: list[dict],
    tools: Sequence[dict] | None = None,
    warnings: list[str] | None = None,
    metadata: LLMExchangeMetadata | None = None,
    db: DB | None = None,
    cache: bool = False,
) -> APIResponse:
    """Make a single Anthropic API call with retry logic.

    If metadata and db are provided, the exchange is automatically saved
    to the database and a trace event is recorded.
    """
    if bool(metadata) != bool(db):
        raise ValueError("metadata and db must be provided together")
    system_prompt = _with_date_suffix(system_prompt)
    kwargs: dict = {
        "model": model,
        "max_tokens": DEFAULT_MAX_TOKENS,
        "system": system_prompt,
        "messages": _add_cache_breakpoint(messages) if cache else messages,
    }
    if _supports_sampling_params(model):
        kwargs["temperature"] = DEFAULT_TEMPERATURE
    if (thinking := _thinking_config(model)) is not None:
        kwargs["thinking"] = thinking
    if (effort := _effort_level(model)) is not None:
        kwargs["output_config"] = {"effort": effort}
    if tools:
        kwargs["tools"] = tools

    n_tools = len(tools) if tools else 0
    log.debug(
        "API call: model=%s, tools=%d, system_prompt_len=%d, messages=%d",
        model,
        n_tools,
        len(system_prompt),
        len(messages),
    )

    @_api_retry
    async def _do_api_call() -> anthropic.types.Message:
        start = time.monotonic()
        response = await client.messages.create(**kwargs)
        elapsed = int((time.monotonic() - start) * 1000)
        response._elapsed_ms = elapsed  # type: ignore[attr-defined]
        return response

    try:
        response = await _do_api_call()
    except Exception as e:
        log.error("API call failed: %s", e, exc_info=True)
        trace = get_trace()
        if trace:
            phase = metadata.phase if metadata else "api_call"
            await trace.record(
                ErrorEvent(
                    message=f"API call failed: {type(e).__name__}: {e}",
                    phase=phase,
                )
            )
        raise

    elapsed_ms: int = getattr(response, "_elapsed_ms", 0)
    log.debug(
        "API response: stop_reason=%s, usage=%d/%d tokens, duration=%dms, full_usage=%s",
        response.stop_reason,
        response.usage.input_tokens,
        response.usage.output_tokens,
        elapsed_ms,
        response.usage,
    )
    if metadata and db:
        text_parts = []
        tool_call_data = []
        for block in response.content:
            if isinstance(block, TextBlock):
                text_parts.append(block.text)
            elif isinstance(block, (ToolUseBlock, ServerToolUseBlock)):
                tool_call_data.append({"name": block.name, "input": block.input})
            elif isinstance(block, WebSearchToolResultBlock):
                tool_call_data.append(
                    {
                        "type": "web_search_tool_result",
                        "tool_use_id": block.tool_use_id,
                        "content": block.model_dump(mode="json")["content"],
                    }
                )
        serialized = _serialize_messages(messages) if len(messages) > 1 else None
        try:
            await _save_exchange(
                metadata,
                db=db,
                model=model,
                system_prompt=system_prompt,
                response_text="\n".join(text_parts) or None,
                tool_calls=tool_call_data,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                duration_ms=elapsed_ms,
                cache_creation_input_tokens=getattr(
                    response.usage, "cache_creation_input_tokens", 0
                )
                or 0,
                cache_read_input_tokens=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
                user_messages=serialized,
            )
        except Exception as exc:
            log.error(
                "Failed to save exchange for call %s: %s",
                metadata.call_id[:8],
                exc,
                exc_info=True,
            )
            trace = get_trace()
            if trace:
                await trace.record(
                    ErrorEvent(
                        message=(f"Failed to save exchange: {type(exc).__name__}: {exc}"),
                        phase=metadata.phase,
                    )
                )
    return APIResponse(message=response, duration_ms=elapsed_ms)


async def text_call(
    system_prompt: str,
    user_message: str = "",
    *,
    messages: list[dict] | None = None,
    metadata: LLMExchangeMetadata | None = None,
    db: DB | None = None,
) -> str:
    """Make a plain text LLM call. Returns the raw text response.

    Pass `messages` for multi-turn conversations, or `user_message` for single-turn.
    Pass `metadata` and `db` together to persist the exchange and record a
    trace event against the call identified by `metadata.call_id`.
    """
    settings = get_settings()
    api_key = settings.require_anthropic_key()
    model = settings.model
    client = anthropic.AsyncAnthropic(api_key=api_key)
    msg_list = messages if messages is not None else [{"role": "user", "content": user_message}]
    if metadata is not None and metadata.user_message is None:
        metadata.user_message = user_message
    log.debug("text_call: messages=%d", len(msg_list))
    api_resp = await call_api(
        client,
        model,
        system_prompt,
        msg_list,
        metadata=metadata,
        db=db,
    )
    for block in api_resp.message.content:
        if isinstance(block, TextBlock):
            log.debug("text_call returned %d chars", len(block.text))
            return block.text
    log.debug("text_call returned empty response")
    return ""


T = TypeVar("T", bound=BaseModel)


@dataclass
class StructuredCallResult(Generic[T]):
    """Result of a structured_call invocation.

    `parsed` holds the validated pydantic instance, or None if the model
    returned no parseable output. The type parameter matches the
    `response_model` passed to `structured_call`.
    """

    parsed: T | None = None
    response_text: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    duration_ms: int | None = None


async def _structured_call_cached(
    system_prompt: str,
    response_model: type[T],
    msg_list: list[dict],
    *,
    tools: Sequence[dict] | None = None,
    metadata: LLMExchangeMetadata | None = None,
    db: DB | None = None,
    model: str | None = None,
) -> StructuredCallResult[T]:
    """Structured output via create() + manual JSON parsing for cache reuse.

    Uses call_api (messages.create) so the request shares the same cache
    namespace as agent loop calls. Injects the JSON schema into the last
    user message and validates the response with pydantic.
    """
    settings = get_settings()
    client = anthropic.AsyncAnthropic(api_key=settings.require_anthropic_key())
    schema_text = _schema_instruction(response_model)
    inject_msgs = _inject_into_last_user_message(msg_list, schema_text)
    effective_model = model or settings.model

    max_parse_attempts = 2
    for parse_attempt in range(max_parse_attempts):
        api_resp = await call_api(
            client,
            effective_model,
            system_prompt,
            inject_msgs,
            tools=tools,
            metadata=metadata,
            db=db,
            cache=True,
        )
        response_text = ""
        for block in api_resp.message.content:
            if isinstance(block, TextBlock):
                response_text += block.text

        try:
            raw = _extract_json(response_text)
            parsed = response_model.model_validate(raw)
            model_name = response_model.__name__
            log.debug(
                "structured_call (cached) success: %s, usage=%d/%d tokens",
                model_name,
                api_resp.message.usage.input_tokens,
                api_resp.message.usage.output_tokens,
            )
            return StructuredCallResult(
                parsed=parsed,
                response_text=response_text or None,
                input_tokens=api_resp.message.usage.input_tokens,
                output_tokens=api_resp.message.usage.output_tokens,
                duration_ms=api_resp.duration_ms,
            )
        except (json.JSONDecodeError, ValueError, ValidationError) as exc:
            if parse_attempt < max_parse_attempts - 1:
                log.warning(
                    "structured_call (cached): parse attempt %d failed (%s), retrying",
                    parse_attempt + 1,
                    exc,
                )
                inject_msgs = list(inject_msgs)
                inject_msgs.append(
                    {
                        "role": "assistant",
                        "content": response_text,
                    }
                )
                inject_msgs.append(
                    {
                        "role": "user",
                        "content": (
                            "Your previous response could not be parsed as valid JSON "
                            "matching the schema. Please try again, responding with ONLY "
                            "the JSON object."
                        ),
                    }
                )
                continue
            log.warning(
                "structured_call (cached): all parse attempts failed (%s), returning empty result",
                exc,
            )
            trace = get_trace()
            if trace:
                phase = metadata.phase if metadata else "structured_call"
                await trace.record(
                    ErrorEvent(
                        message=f"Structured call parse failed: {exc}",
                        phase=phase,
                    )
                )
            return StructuredCallResult(
                response_text=response_text or None,
                input_tokens=api_resp.message.usage.input_tokens,
                output_tokens=api_resp.message.usage.output_tokens,
                duration_ms=api_resp.duration_ms,
            )

    raise RuntimeError("Unreachable: parse retry loop exhausted")


def _inject_into_last_user_message(
    messages: list[dict],
    extra_text: str,
) -> list[dict]:
    """Append extra_text to the last user message's content."""
    msgs = list(messages)
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i].get("role") == "user":
            last = dict(msgs[i])
            content = last.get("content")
            if isinstance(content, str):
                last["content"] = content + extra_text
            elif isinstance(content, list):
                content = list(content)
                content.append({"type": "text", "text": extra_text})
                last["content"] = content
            msgs[i] = last
            return msgs
    msgs.append({"role": "user", "content": extra_text})
    return msgs


async def _structured_call_parse(
    system_prompt: str,
    response_model: type[T] | None,
    msg_list: list[dict],
    *,
    tools: Sequence[dict] | None = None,
    tool_choice: dict | None = None,
    metadata: LLMExchangeMetadata | None = None,
    db: DB | None = None,
    model: str | None = None,
    max_tokens: int | None = None,
) -> StructuredCallResult[T]:
    """Structured output via messages.parse (no cache sharing with create)."""
    settings = get_settings()
    client = anthropic.AsyncAnthropic(api_key=settings.require_anthropic_key())
    model = model or settings.model
    system_prompt = _with_date_suffix(system_prompt)

    parse_kwargs: dict = {
        "model": model,
        "max_tokens": max_tokens if max_tokens is not None else DEFAULT_MAX_TOKENS,
        "system": system_prompt,
        "messages": msg_list,
    }
    if _supports_sampling_params(model):
        parse_kwargs["temperature"] = DEFAULT_TEMPERATURE
    if (thinking := _thinking_config(model)) is not None:
        parse_kwargs["thinking"] = thinking
    if (effort := _effort_level(model)) is not None:
        parse_kwargs["output_config"] = {"effort": effort}
    if response_model is not None:
        parse_kwargs["output_format"] = response_model
    if tools is not None:
        parse_kwargs["tools"] = tools
    if tool_choice is not None:
        parse_kwargs["tool_choice"] = tool_choice

    @_api_retry
    async def _do_parse() -> Any:
        t0 = time.monotonic()
        resp = await client.messages.parse(**parse_kwargs)
        resp._elapsed_ms = int((time.monotonic() - t0) * 1000)  # type: ignore[attr-defined]
        return resp

    response: Any = await _do_parse()
    elapsed_ms: int = getattr(response, "_elapsed_ms", 0)
    response_text = ""
    for block in response.content:
        if isinstance(block, TextBlock):
            response_text += block.text
    if metadata and db:
        serialized: Sequence[dict] | None = None
        if metadata.user_message is None:
            if len(msg_list) == 1:
                content = msg_list[0].get("content", "")
                metadata.user_message = content if isinstance(content, str) else None
            if len(msg_list) > 1 or metadata.user_message is None:
                serialized = _serialize_messages(msg_list)
        await _save_exchange(
            metadata,
            db=db,
            model=model,
            system_prompt=system_prompt,
            response_text=response_text or None,
            tool_calls=[],
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            duration_ms=elapsed_ms,
            cache_creation_input_tokens=getattr(response.usage, "cache_creation_input_tokens", 0)
            or 0,
            cache_read_input_tokens=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
            user_messages=serialized,
        )
    if response.parsed_output is not None:
        log.debug(
            "structured_call success: %s, usage=%d/%d tokens",
            response_model.__name__ if response_model else "unknown",
            response.usage.input_tokens,
            response.usage.output_tokens,
        )
        return StructuredCallResult(
            parsed=response.parsed_output,
            response_text=response_text or None,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            duration_ms=elapsed_ms,
        )
    log.warning(
        "Structured output was empty (stop_reason=%s, usage=%d tokens)",
        response.stop_reason,
        response.usage.output_tokens,
    )
    return StructuredCallResult(
        response_text=response_text or None,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        duration_ms=elapsed_ms,
    )


@overload
async def structured_call(
    system_prompt: str,
    user_message: str,
    response_model: type[T],
    *,
    messages: list[dict] | None = None,
    tools: Sequence[dict] | None = None,
    tool_choice: dict | None = None,
    metadata: LLMExchangeMetadata | None = None,
    db: DB | None = None,
    cache: bool = False,
    model: str | None = None,
) -> StructuredCallResult[T]: ...


@overload
async def structured_call(
    system_prompt: str,
    user_message: str = "",
    *,
    response_model: type[T],
    messages: list[dict] | None = None,
    tools: Sequence[dict] | None = None,
    tool_choice: dict | None = None,
    metadata: LLMExchangeMetadata | None = None,
    db: DB | None = None,
    cache: bool = False,
    model: str | None = None,
) -> StructuredCallResult[T]: ...


@overload
async def structured_call(
    system_prompt: str,
    user_message: str = "",
    response_model: None = None,
    *,
    messages: list[dict] | None = None,
    tools: Sequence[dict] | None = None,
    tool_choice: dict | None = None,
    metadata: LLMExchangeMetadata | None = None,
    db: DB | None = None,
    cache: bool = False,
    model: str | None = None,
) -> StructuredCallResult[BaseModel]: ...


async def structured_call(
    system_prompt: str,
    user_message: str = "",
    response_model: type[T] | None = None,
    *,
    messages: list[dict] | None = None,
    tools: Sequence[dict] | None = None,
    tool_choice: dict | None = None,
    metadata: LLMExchangeMetadata | None = None,
    db: DB | None = None,
    cache: bool = False,
    model: str | None = None,
    max_tokens: int | None = None,
) -> StructuredCallResult[T] | StructuredCallResult[BaseModel]:
    """Run an LLM call that returns structured output matching response_model.

    When cache=True, uses messages.create with manual JSON parsing so the
    request shares the same cache namespace as agent loop calls. Otherwise
    uses messages.parse with output_format for guaranteed schema adherence.

    Pass `messages` for multi-turn conversations, or `user_message` for single-turn.
    Pass `tools` to share cache prefix with agent calls.

    Pass ``max_tokens`` to override the default output budget. Long-form
    artefact generation in particular can outgrow the default; bump this
    when the expected output is known to be large.
    """
    if bool(metadata) != bool(db):
        raise ValueError("metadata and db must be provided together")
    if not user_message and not messages:
        raise ValueError("Either user_message or messages must be provided")

    raw_msgs = messages if messages is not None else [{"role": "user", "content": user_message}]
    model_name = response_model.__name__ if response_model else "None"
    log.debug(
        "structured_call: response_model=%s, cache=%s",
        model_name,
        cache,
    )

    if cache and response_model is not None:
        if max_tokens is not None:
            raise ValueError(
                "max_tokens is not supported on the cached (cache=True) path yet; "
                "plumb it through call_api if you need it."
            )
        return await _structured_call_cached(
            system_prompt,
            response_model,
            raw_msgs,
            tools=tools,
            metadata=metadata,
            db=db,
            model=model,
        )
    return await _structured_call_parse(
        system_prompt,
        response_model,
        raw_msgs,
        tools=tools,
        tool_choice=tool_choice,
        metadata=metadata,
        db=db,
        model=model,
        max_tokens=max_tokens,
    )
