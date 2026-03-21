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
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from collections.abc import Awaitable, Callable

import anthropic
from anthropic.types import MessageParam, ServerToolUseBlock, TextBlock, ToolUseBlock
from pydantic import BaseModel, ValidationError

from rumil.pricing import compute_cost

from rumil.settings import get_settings
from rumil.tracing.trace_events import LLMExchangeEvent

if TYPE_CHECKING:
    from rumil.database import DB
    from rumil.tracing.tracer import CallTrace

DEFAULT_MAX_TOKENS = 20_000

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


_CACHE_BREAKPOINT = {"type": "ephemeral"}


def _add_cache_breakpoint(messages: list[dict]) -> list[dict]:
    """Return a shallow copy of messages with a cache breakpoint on the last block.

    Mutates nothing — copies only the last message and its content.
    """
    if not messages:
        return messages
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
            content[-1] = {**last_block.model_dump(), "cache_control": _CACHE_BREAKPOINT}
        last["content"] = content
    msgs[-1] = last
    return msgs


_JSON_BLOCK_RE = re.compile(r'```(?:json)?\s*\n(.*?)\n```', re.DOTALL)


def _extract_json(text: str) -> dict:
    """Extract a JSON object from LLM response text.

    Tries fenced code blocks first, then bare JSON.
    """
    m = _JSON_BLOCK_RE.search(text)
    if m:
        return json.loads(m.group(1))
    stripped = text.strip()
    start = stripped.find('{')
    if start != -1:
        return json.loads(stripped[start:stripped.rfind('}') + 1])
    raise ValueError(f'No JSON found in response: {text[:200]}')


def _serialize_messages(messages: list[dict]) -> list[dict]:
    """Serialize messages for JSON storage, converting SDK objects to dicts."""
    result = []
    for msg in messages:
        out: dict = {'role': msg['role']}
        content = msg.get('content')
        if isinstance(content, str):
            out['content'] = content
        elif isinstance(content, list):
            blocks = []
            for block in content:
                if isinstance(block, dict):
                    blocks.append(block)
                elif hasattr(block, 'model_dump'):
                    blocks.append(block.model_dump())
                else:
                    blocks.append(str(block))
            out['content'] = blocks
        else:
            out['content'] = str(content) if content is not None else None
        result.append(out)
    return result


def _schema_instruction(response_model: type[BaseModel]) -> str:
    """Build a JSON schema instruction block for the model."""
    schema = response_model.model_json_schema()
    return (
        '\n\nRespond with ONLY a JSON object matching this schema '
        '(no other text, no markdown fences):\n'
        + json.dumps(schema, indent=2)
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
    """

    call_id: str
    phase: str
    trace: CallTrace | None = None
    round_num: int | None = None
    user_message: str | None = None
    user_messages: list[dict] | None = None


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
        user_messages=metadata.user_messages,
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
    kwargs: dict = {
        "model": model,
        "max_tokens": DEFAULT_MAX_TOKENS,
        "system": system_prompt,
        "messages": _add_cache_breakpoint(messages) if cache else messages,
    }
    if tools:
        kwargs["tools"] = tools

    n_tools = len(tools) if tools else 0
    log.debug(
        "API call: model=%s, tools=%d, system_prompt_len=%d, messages=%d",
        model, n_tools, len(system_prompt), len(messages),
    )

    for attempt in range(MAX_API_RETRIES):
        try:
            start = time.monotonic()
            response = await client.messages.create(**kwargs)
            elapsed_ms = int((time.monotonic() - start) * 1000)
            log.debug(
                "API response: stop_reason=%s, usage=%d/%d tokens, duration=%dms, "
                "full_usage=%s",
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
                        tool_call_data.append(
                            {"name": block.name, "input": block.input}
                        )
                if metadata.user_messages is None and len(messages) > 1:
                    metadata.user_messages = _serialize_messages(messages)
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
    log.debug("text_call: messages=%d", len(msg_list))
    api_resp = await call_api(client, model, system_prompt, msg_list)
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


async def _structured_call_cached(
    system_prompt: str,
    response_model: type[BaseModel],
    msg_list: list[dict],
    *,
    tools: list[dict] | None = None,
    metadata: LLMExchangeMetadata | None = None,
    db: DB | None = None,
) -> StructuredCallResult:
    """Structured output via create() + manual JSON parsing for cache reuse.

    Uses call_api (messages.create) so the request shares the same cache
    namespace as agent loop calls. Injects the JSON schema into the last
    user message and validates the response with pydantic.
    """
    settings = get_settings()
    client = anthropic.AsyncAnthropic(api_key=settings.require_anthropic_key())
    schema_text = _schema_instruction(response_model)
    inject_msgs = _inject_into_last_user_message(msg_list, schema_text)

    max_parse_attempts = 2
    for parse_attempt in range(max_parse_attempts):
        api_resp = await call_api(
            client, settings.model, system_prompt, inject_msgs,
            tools=tools,
            metadata=metadata, db=db, cache=True,
        )
        response_text = ''
        for block in api_resp.message.content:
            if isinstance(block, TextBlock):
                response_text += block.text

        try:
            raw = _extract_json(response_text)
            parsed = response_model.model_validate(raw)
            model_name = response_model.__name__
            log.debug(
                'structured_call (cached) success: %s, usage=%d/%d tokens',
                model_name,
                api_resp.message.usage.input_tokens,
                api_resp.message.usage.output_tokens,
            )
            return StructuredCallResult(
                data=parsed.model_dump(),
                response_text=response_text or None,
                input_tokens=api_resp.message.usage.input_tokens,
                output_tokens=api_resp.message.usage.output_tokens,
                duration_ms=api_resp.duration_ms,
            )
        except (json.JSONDecodeError, ValueError, ValidationError) as exc:
            if parse_attempt < max_parse_attempts - 1:
                log.warning(
                    'structured_call (cached): parse attempt %d failed (%s), retrying',
                    parse_attempt + 1, exc,
                )
                inject_msgs = list(inject_msgs)
                inject_msgs.append({
                    'role': 'assistant', 'content': response_text,
                })
                inject_msgs.append({
                    'role': 'user',
                    'content': (
                        'Your previous response could not be parsed as valid JSON '
                        'matching the schema. Please try again, responding with ONLY '
                        'the JSON object.'
                    ),
                })
                continue
            log.warning(
                'structured_call (cached): all parse attempts failed (%s), '
                'returning empty result',
                exc,
            )
            return StructuredCallResult(
                response_text=response_text or None,
                input_tokens=api_resp.message.usage.input_tokens,
                output_tokens=api_resp.message.usage.output_tokens,
                duration_ms=api_resp.duration_ms,
            )

    raise RuntimeError('Unreachable: parse retry loop exhausted')


def _inject_into_last_user_message(
    messages: list[dict], extra_text: str,
) -> list[dict]:
    """Append extra_text to the last user message's content."""
    msgs = list(messages)
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i].get('role') == 'user':
            last = dict(msgs[i])
            content = last.get('content')
            if isinstance(content, str):
                last['content'] = content + extra_text
            elif isinstance(content, list):
                content = list(content)
                content.append({'type': 'text', 'text': extra_text})
                last['content'] = content
            msgs[i] = last
            return msgs
    msgs.append({'role': 'user', 'content': extra_text})
    return msgs


async def _structured_call_parse(
    system_prompt: str,
    response_model: type[BaseModel] | None,
    msg_list: list[dict],
    *,
    tools: list[dict] | None = None,
    tool_choice: dict | None = None,
    metadata: LLMExchangeMetadata | None = None,
    db: DB | None = None,
) -> StructuredCallResult:
    """Structured output via messages.parse (no cache sharing with create)."""
    settings = get_settings()
    client = anthropic.AsyncAnthropic(api_key=settings.require_anthropic_key())
    model = settings.model

    for attempt in range(MAX_API_RETRIES):
        try:
            t0 = time.monotonic()
            parse_kwargs: dict = {
                'model': model,
                'max_tokens': DEFAULT_MAX_TOKENS,
                'system': system_prompt,
                'messages': msg_list,
            }
            if response_model is not None:
                parse_kwargs['output_format'] = response_model
            if tools is not None:
                parse_kwargs['tools'] = tools
            if tool_choice is not None:
                parse_kwargs['tool_choice'] = tool_choice
            response = await client.messages.parse(**parse_kwargs)
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            response_text = ''
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
                    cache_creation_input_tokens=getattr(response.usage, 'cache_creation_input_tokens', 0) or 0,
                    cache_read_input_tokens=getattr(response.usage, 'cache_read_input_tokens', 0) or 0,
                )
            if response.parsed_output is not None:
                log.debug(
                    'structured_call success: %s, usage=%d/%d tokens',
                    response_model.__name__ if response_model else 'unknown',
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
                'Structured output was empty (stop_reason=%s, usage=%d tokens)',
                response.stop_reason, response.usage.output_tokens,
            )
            return StructuredCallResult(
                response_text=response_text or None,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                duration_ms=elapsed_ms,
            )
        except Exception as e:
            status = getattr(e, 'status_code', None)
            name = type(e).__name__.lower()
            retryable = (
                status in (429, 500, 529)
                or 'overloaded' in name
                or 'ratelimit' in name
                or 'internalserver' in name
                or 'overloaded' in str(e).lower()
            )
            if not retryable or attempt == MAX_API_RETRIES - 1:
                log.error(
                    'structured_call failed (non-retryable): %s', e, exc_info=True,
                )
                raise
            wait = 2**attempt
            label = f'HTTP {status}' if status else name
            log.warning(
                'structured_call: API temporarily unavailable (%s), '
                'retrying in %ds (attempt %d/%d)',
                label, wait, attempt + 1, MAX_API_RETRIES,
            )
            await asyncio.sleep(wait)

    raise RuntimeError('Unreachable: retry loop exhausted without raising')


async def structured_call(
    system_prompt: str,
    user_message: str = '',
    response_model: type[BaseModel] | None = None,
    *,
    messages: list[dict] | None = None,
    tools: list[dict] | None = None,
    tool_choice: dict | None = None,
    metadata: LLMExchangeMetadata | None = None,
    db: DB | None = None,
    cache: bool = False,
) -> StructuredCallResult:
    """Run an LLM call that returns structured output matching response_model.

    When cache=True, uses messages.create with manual JSON parsing so the
    request shares the same cache namespace as agent loop calls. Otherwise
    uses messages.parse with output_format for guaranteed schema adherence.

    Pass `messages` for multi-turn conversations, or `user_message` for single-turn.
    Pass `tools` to share cache prefix with agent calls.
    """
    if bool(metadata) != bool(db):
        raise ValueError('metadata and db must be provided together')
    if not user_message and not messages:
        raise ValueError('Either user_message or messages must be provided')

    raw_msgs = (
        messages if messages is not None
        else [{'role': 'user', 'content': user_message}]
    )
    model_name = response_model.__name__ if response_model else 'None'
    log.debug(
        'structured_call: response_model=%s, cache=%s',
        model_name, cache,
    )

    if cache and response_model is not None:
        return await _structured_call_cached(
            system_prompt, response_model, raw_msgs,
            tools=tools,
            metadata=metadata, db=db,
        )
    return await _structured_call_parse(
        system_prompt, response_model, raw_msgs,
        tools=tools, tool_choice=tool_choice,
        metadata=metadata, db=db,
    )
