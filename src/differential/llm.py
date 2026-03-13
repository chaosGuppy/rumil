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

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from collections.abc import Awaitable, Callable

import anthropic
from anthropic.types import MessageParam, TextBlock, ToolUseBlock
from pydantic import BaseModel

from differential.pricing import compute_cost
from differential.settings import get_settings
from differential.tracing.trace_events import LLMExchangeEvent

if TYPE_CHECKING:
    from differential.database import DB
    from differential.tracing.tracer import CallTrace

PROMPTS_DIR = Path(__file__).parent.parent.parent / "prompts"

log = logging.getLogger(__name__)

MAX_API_RETRIES = 4


def _load_file(name: str) -> str:
    path = PROMPTS_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {path}")
    return path.read_text(encoding="utf-8")


def build_system_prompt(call_type: str) -> str:
    """Combine preamble + call-type instructions into one system prompt."""
    preamble = _load_file("preamble.md")
    instructions = _load_file(f"{call_type}.md")
    return f"{preamble}\n\n---\n\n{instructions}"


def build_user_message(context_text: str, task_description: str) -> str:
    """Combine context dump + specific task into one user message."""
    if context_text:
        return f"{context_text}\n\n---\n\n{task_description}"
    return task_description


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


@dataclass
class LLMExchangeMetadata:
    """Context for automatically saving an LLM exchange to the database.

    Encapsulates the parameters needed by save_llm_exchange that are not
    already present in the call_api / structured_call signatures.
    """

    call_id: str
    phase: str
    trace: CallTrace | None = None
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
    )
    cost_usd = compute_cost(
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
    )
    if metadata.trace:
        await metadata.trace.record(LLMExchangeEvent(
            exchange_id=exchange_id,
            phase=metadata.phase,
            round=metadata.round_num,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=cache_creation_input_tokens or None,
            cache_read_input_tokens=cache_read_input_tokens or None,
            duration_ms=duration_ms,
            cost_usd=cost_usd or None,
        ))


async def call_api(
    client: anthropic.AsyncAnthropic,
    model: str,
    system_prompt: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    max_tokens: int = 4096,
    warnings: list[str] | None = None,
    metadata: LLMExchangeMetadata | None = None,
    db: DB | None = None,
) -> APIResponse:
    """Make a single Anthropic API call with retry logic.

    If metadata and db are provided, the exchange is automatically saved
    to the database and a trace event is recorded.
    """
    if bool(metadata) != bool(db):
        raise ValueError("metadata and db must be provided together")
    kwargs: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": messages,
    }
    if tools:
        kwargs["tools"] = tools

    n_tools = len(tools) if tools else 0
    log.debug(
        "API call: model=%s, max_tokens=%d, tools=%d, system_prompt_len=%d, messages=%d",
        model, max_tokens, n_tools, len(system_prompt), len(messages),
    )

    for attempt in range(MAX_API_RETRIES):
        try:
            start = time.monotonic()
            response = await client.messages.create(**kwargs)
            elapsed_ms = int((time.monotonic() - start) * 1000)
            log.debug(
                "API response: stop_reason=%s, usage=%d/%d tokens, duration=%dms",
                response.stop_reason,
                response.usage.input_tokens,
                response.usage.output_tokens,
                elapsed_ms,
            )
            if metadata and db:
                text_parts = []
                tool_call_data = []
                for block in response.content:
                    if isinstance(block, TextBlock):
                        text_parts.append(block.text)
                    elif isinstance(block, ToolUseBlock):
                        tool_call_data.append(
                            {"name": block.name, "input": block.input}
                        )
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
                    cache_creation_input_tokens=getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
                    cache_read_input_tokens=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
                )
            return APIResponse(message=response, duration_ms=elapsed_ms)
        except Exception as e:
            status = getattr(e, "status_code", None)
            name = type(e).__name__.lower()
            retryable = (
                status in (429, 500, 529)
                or "overloaded" in name
                or "ratelimit" in name
                or "internalserver" in name
                or "overloaded" in str(e).lower()
            )
            if not retryable or attempt == MAX_API_RETRIES - 1:
                log.error("API call failed (non-retryable): %s", e, exc_info=True)
                raise
            wait = 2**attempt
            label = f"HTTP {status}" if status else name
            msg = (
                f"API temporarily unavailable ({label}), "
                f"retrying in {wait}s (attempt {attempt + 1}/{MAX_API_RETRIES})"
            )
            log.warning("%s", msg)
            if warnings is not None:
                warnings.append(msg)
            await asyncio.sleep(wait)

    raise RuntimeError("Unreachable: retry loop exhausted without raising")


async def text_call(
    system_prompt: str,
    user_message: str = "",
    *,
    messages: list[dict] | None = None,
    max_tokens: int = 4096,
) -> str:
    """Make a plain text LLM call. Returns the raw text response.

    Pass `messages` for multi-turn conversations, or `user_message` for single-turn.
    """
    settings = get_settings()
    api_key = settings.require_anthropic_key()
    model = settings.model
    client = anthropic.AsyncAnthropic(api_key=api_key)
    msg_list = (
        messages
        if messages is not None
        else [{"role": "user", "content": user_message}]
    )
    log.debug("text_call: max_tokens=%d, messages=%d", max_tokens, len(msg_list))
    api_resp = await call_api(client, model, system_prompt, msg_list, max_tokens=max_tokens)
    for block in api_resp.message.content:
        if isinstance(block, TextBlock):
            log.debug("text_call returned %d chars", len(block.text))
            return block.text
    log.debug("text_call returned empty response")
    return ""


@dataclass
class StructuredCallResult:
    """Result of a structured_call invocation."""

    data: dict | None = None
    response_text: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    duration_ms: int | None = None


async def structured_call(
    system_prompt: str,
    user_message: str,
    response_model: type[BaseModel],
    *,
    max_tokens: int = 1024,
    metadata: LLMExchangeMetadata | None = None,
    db: DB | None = None,
) -> StructuredCallResult:
    """Run an LLM call that returns structured output matching response_model.

    Uses the Anthropic structured output API (messages.parse with output_format).
    Returns a StructuredCallResult with the parsed data and usage metadata.
    """
    if bool(metadata) != bool(db):
        raise ValueError("metadata and db must be provided together")
    settings = get_settings()
    api_key = settings.require_anthropic_key()
    model = settings.model
    client = anthropic.AsyncAnthropic(api_key=api_key)

    messages: list[MessageParam] = [{"role": "user", "content": user_message}]
    log.debug(
        "structured_call: model=%s, response_model=%s, max_tokens=%d",
        model, response_model.__name__, max_tokens,
    )

    for attempt in range(MAX_API_RETRIES):
        try:
            t0 = time.monotonic()
            response = await client.messages.parse(
                model=model,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=messages,
                output_format=response_model,
            )
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            response_text = ""
            for block in response.content:
                if isinstance(block, TextBlock):
                    response_text += block.text
            if metadata and db:
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
                    cache_creation_input_tokens=getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
                    cache_read_input_tokens=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
                )
            if response.parsed_output is not None:
                log.debug(
                    "structured_call success: %s, usage=%d/%d tokens",
                    response_model.__name__,
                    response.usage.input_tokens,
                    response.usage.output_tokens,
                )
                return StructuredCallResult(
                    data=response.parsed_output.model_dump(),
                    response_text=response_text or None,
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    duration_ms=elapsed_ms,
                )
            log.warning(
                "Structured output was empty (stop_reason=%s, usage=%d/%d tokens)",
                response.stop_reason, response.usage.output_tokens, max_tokens,
            )
            return StructuredCallResult(
                response_text=response_text or None,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                duration_ms=elapsed_ms,
            )
        except Exception as e:
            status = getattr(e, "status_code", None)
            name = type(e).__name__.lower()
            retryable = (
                status in (429, 500, 529)
                or "overloaded" in name
                or "ratelimit" in name
                or "internalserver" in name
                or "overloaded" in str(e).lower()
            )
            if not retryable or attempt == MAX_API_RETRIES - 1:
                log.error(
                    "structured_call failed (non-retryable): %s", e, exc_info=True,
                )
                raise
            wait = 2**attempt
            label = f"HTTP {status}" if status else name
            log.warning(
                "structured_call: API temporarily unavailable (%s), "
                "retrying in %ds (attempt %d/%d)",
                label, wait, attempt + 1, MAX_API_RETRIES,
            )
            await asyncio.sleep(wait)

    raise RuntimeError("Unreachable: retry loop exhausted without raising")
