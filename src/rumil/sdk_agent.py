"""Shared infrastructure for running Claude Agent SDK agents with tracing."""

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AgentDefinition,
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookContext,
    HookInput,
    HookMatcher,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
)
from claude_agent_sdk.types import HookEvent, SyncHookJSONOutput

from rumil.database import DB
from rumil.models import Call, CallStatus, CallType
from rumil.pricing import compute_cost
from rumil.settings import get_settings
from rumil.tracing.broadcast import Broadcaster
from rumil.tracing.trace_events import (
    AgentStartedEvent,
    LLMExchangeEvent,
    SubagentCompletedEvent,
    SubagentStartedEvent,
    ToolCallEvent,
    WarningEvent,
)
from rumil.tracing.tracer import CallTrace

log = logging.getLogger(__name__)


@dataclass
class SdkAgentConfig:
    """Configuration for running a Claude Agent SDK agent."""

    system_prompt: str
    user_prompt: str
    server_name: str
    mcp_tools: Sequence
    call: Call
    call_type: CallType
    scope_page_id: str
    db: DB
    trace: CallTrace
    broadcaster: Broadcaster | None = None
    allowed_tools: Sequence[str] = ()
    disallowed_tools: Sequence[str] = ("Write", "Edit", "Bash", "Glob")
    agents: dict[str, AgentDefinition] = field(default_factory=dict)
    extra_hooks: dict[HookEvent, list[HookMatcher]] = field(default_factory=dict)
    output_format: dict[str, Any] | None = None


@dataclass
class SdkAgentResult:
    """Result from running a Claude Agent SDK agent."""

    last_assistant_text: Sequence[str]
    all_assistant_text: Sequence[str] = field(default_factory=list)
    structured_output: Any = None


@dataclass
class _TurnUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    response_text: str | None = None
    tool_calls: list[dict] = field(default_factory=list)


@dataclass
class _TranscriptSummary:
    last_text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    turns: list[_TurnUsage] = field(default_factory=list)


def _read_subagent_transcript(transcript_path: str, max_text_len: int = 500) -> _TranscriptSummary:
    """Parse a subagent transcript JSONL for the last text and per-turn usage."""
    result = _TranscriptSummary()
    if not transcript_path:
        log.warning("Subagent transcript path is empty — no usage data will be captured")
        return result
    path = Path(transcript_path)
    if not path.exists():
        log.warning("Subagent transcript file does not exist: %s", transcript_path)
        return result
    try:
        lines = path.read_text().splitlines()
    except Exception as exc:
        log.warning(
            "Failed to read subagent transcript %s: %s: %s",
            transcript_path,
            type(exc).__name__,
            exc,
        )
        return result
    for line in lines:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("type") == "assistant":
            turn_text_parts: list[str] = []
            turn_tool_calls: list[dict] = []
            for block in msg.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    result.last_text = block["text"]
                    turn_text_parts.append(block["text"])
                elif block.get("type") == "tool_use":
                    turn_tool_calls.append(
                        {"name": block.get("name", ""), "input": block.get("input", {})}
                    )
            usage = msg.get("message", {}).get("usage")
            if isinstance(usage, dict):
                turn = _TurnUsage(
                    input_tokens=usage.get("input_tokens", 0),
                    output_tokens=usage.get("output_tokens", 0),
                    cache_creation_input_tokens=usage.get("cache_creation_input_tokens", 0),
                    cache_read_input_tokens=usage.get("cache_read_input_tokens", 0),
                    response_text="\n".join(turn_text_parts) if turn_text_parts else None,
                    tool_calls=turn_tool_calls,
                )
                result.turns.append(turn)
                result.input_tokens += turn.input_tokens
                result.output_tokens += turn.output_tokens
                result.cache_creation_input_tokens += turn.cache_creation_input_tokens
                result.cache_read_input_tokens += turn.cache_read_input_tokens
    if len(result.last_text) > max_text_len:
        result.last_text = result.last_text[:max_text_len]
    if not result.turns:
        log.warning(
            "Subagent transcript %s contained no assistant messages with usage data (%d lines)",
            transcript_path,
            len(lines),
        )
    return result


def _read_subagent_summary(transcript_path: str, max_len: int = 500) -> str:
    """Extract the last assistant text from a subagent transcript file."""
    return _read_subagent_transcript(transcript_path, max_text_len=max_len).last_text


def extract_response_text(input_data: HookInput) -> str:
    """Extract text from a tool response in hook input_data."""
    tool_response = input_data.get("tool_response", None)  # type: ignore[call-overload]
    if isinstance(tool_response, dict):
        for block in tool_response.get("content", []):
            if isinstance(block, dict) and block.get("type") == "text":
                return block["text"]
    elif isinstance(tool_response, str):
        return tool_response
    return ""


async def run_sdk_agent(config: SdkAgentConfig) -> SdkAgentResult:
    """Run a Claude Agent SDK agent with standard hooks and tracing.

    Handles subagent lifecycle (call creation, tracing, completion),
    tool call tracing for all tools, and the client response loop.
    """
    settings = get_settings()
    server = create_sdk_mcp_server(config.server_name, tools=list(config.mcp_tools))
    tool_fqnames = [f"mcp__{config.server_name}__{t.name}" for t in config.mcp_tools]

    subagent_calls: dict[str, str] = {}
    subagent_traces: dict[str, CallTrace] = {}
    pending_agent_prompts: list[str] = []
    subagent_count = 0

    def _trace_for_agent(agent_id: str | None) -> CallTrace:
        """Return the child trace for a subagent, or the parent trace."""
        if agent_id and agent_id in subagent_traces:
            return subagent_traces[agent_id]
        return config.trace

    async def on_pre_tool_use(
        input_data: HookInput, tool_use_id: str | None, context: HookContext
    ) -> SyncHookJSONOutput:
        nonlocal subagent_count
        tool_name: str = input_data.get("tool_name", "")  # type: ignore[call-overload]
        if tool_name == "Agent":
            max_subagents = settings.sdk_agent_max_subagents
            if subagent_count >= max_subagents:
                log.warning(
                    "Subagent limit reached (%d/%d), blocking dispatch",
                    subagent_count,
                    max_subagents,
                )
                return SyncHookJSONOutput(
                    decision="block",
                    reason=(
                        f"Subagent limit reached ({subagent_count}/{max_subagents}). "
                        "You cannot dispatch more subagents. Wait for any "
                        "already-dispatched subagents to finish, use their "
                        "findings together with your own work so far, and "
                        "continue with any further work you can do without "
                        "subagents before producing your final output."
                    ),
                )
            subagent_count += 1
            tool_input: dict = input_data.get("tool_input", {})  # type: ignore[call-overload]
            prompt = tool_input.get("prompt", "")
            pending_agent_prompts.append(prompt)
        return SyncHookJSONOutput()

    async def on_post_tool_use(
        input_data: HookInput, tool_use_id: str | None, context: HookContext
    ) -> SyncHookJSONOutput:
        tool_name: str = input_data.get("tool_name", "")  # type: ignore[call-overload]
        tool_input: dict = input_data.get("tool_input", {})  # type: ignore[call-overload]
        agent_id: str = input_data.get("agent_id", "")  # type: ignore[call-overload]
        response = extract_response_text(input_data)
        target_trace = _trace_for_agent(agent_id)
        await target_trace.record(
            ToolCallEvent(
                tool_name=tool_name,
                tool_input=tool_input,
                response=response,
            )
        )
        return SyncHookJSONOutput()

    async def on_subagent_start(
        input_data: HookInput, tool_use_id: str | None, context: HookContext
    ) -> SyncHookJSONOutput:
        agent_id: str = input_data.get("agent_id", "")  # type: ignore[call-overload]
        agent_type: str = input_data.get("agent_type", "")  # type: ignore[call-overload]
        prompt = pending_agent_prompts.pop(0) if pending_agent_prompts else ""
        child_call = await config.db.create_call(
            call_type=config.call_type,
            scope_page_id=config.scope_page_id,
            parent_call_id=config.call.id,
        )
        subagent_calls[agent_id] = child_call.id
        subagent_traces[agent_id] = CallTrace(
            child_call.id, config.db, broadcaster=config.broadcaster
        )
        await config.db.update_call_status(child_call.id, CallStatus.RUNNING)
        await config.trace.record(
            SubagentStartedEvent(
                agent_id=agent_id,
                agent_type=agent_type,
                child_call_id=child_call.id,
                prompt=prompt,
            )
        )
        log.info("Subagent %s started -> child call %s", agent_id, child_call.id)
        return SyncHookJSONOutput()

    async def on_subagent_stop(
        input_data: HookInput, tool_use_id: str | None, context: HookContext
    ) -> SyncHookJSONOutput:
        agent_id: str = input_data.get("agent_id", "")  # type: ignore[call-overload]
        child_call_id = subagent_calls.get(agent_id)
        transcript_path: str = input_data.get("agent_transcript_path", "")  # type: ignore[call-overload]
        transcript = _read_subagent_transcript(transcript_path)
        summary = input_data.get("agent_result", "")  # type: ignore[call-overload]
        if not isinstance(summary, str) or not summary:
            summary = transcript.last_text
        if not isinstance(summary, str):
            summary = ""

        child_trace = subagent_traces.get(agent_id)
        if child_trace and child_call_id and transcript.turns:
            for turn_num, turn in enumerate(transcript.turns, 1):
                turn_cost = compute_cost(
                    model=settings.model,
                    input_tokens=turn.input_tokens,
                    output_tokens=turn.output_tokens,
                    cache_creation_input_tokens=turn.cache_creation_input_tokens,
                    cache_read_input_tokens=turn.cache_read_input_tokens,
                )
                try:
                    exchange_id = await config.db.save_llm_exchange(
                        call_id=child_call_id,
                        phase="subagent",
                        system_prompt=None,
                        user_message=None,
                        response_text=turn.response_text,
                        tool_calls=turn.tool_calls or None,
                        input_tokens=turn.input_tokens,
                        output_tokens=turn.output_tokens,
                        round_num=turn_num,
                        cache_creation_input_tokens=turn.cache_creation_input_tokens or None,
                        cache_read_input_tokens=turn.cache_read_input_tokens or None,
                    )
                    await child_trace.record(
                        LLMExchangeEvent(
                            exchange_id=exchange_id,
                            phase="subagent",
                            round=turn_num,
                            input_tokens=turn.input_tokens,
                            output_tokens=turn.output_tokens,
                            cache_creation_input_tokens=turn.cache_creation_input_tokens or None,
                            cache_read_input_tokens=turn.cache_read_input_tokens or None,
                            cost_usd=turn_cost or None,
                        )
                    )
                except Exception as exc:
                    log.error("Failed to save subagent exchange: %s", exc)
                    await child_trace.record(
                        WarningEvent(
                            message=(
                                f"Failed to persist subagent exchange "
                                f"(round {turn_num}): {type(exc).__name__}: {exc}"
                            )
                        )
                    )
        elif child_trace:
            await child_trace.record(
                WarningEvent(
                    message=(
                        f"Subagent transcript missing or unparseable "
                        f"(path={transcript_path or '<empty>'}); "
                        "per-turn usage and cost not recorded"
                    )
                )
            )

        has_usage = transcript.input_tokens > 0 or transcript.output_tokens > 0
        cost_usd: float | None = None
        if has_usage:
            cost_usd = (
                compute_cost(
                    model=settings.model,
                    input_tokens=transcript.input_tokens,
                    output_tokens=transcript.output_tokens,
                    cache_creation_input_tokens=transcript.cache_creation_input_tokens,
                    cache_read_input_tokens=transcript.cache_read_input_tokens,
                )
                or None
            )
        if child_call_id:
            if child_trace and child_trace.total_cost_usd > 0:
                child_call_cost: float | None = child_trace.total_cost_usd
            else:
                child_call_cost = cost_usd
            await config.db.update_call_status(
                child_call_id,
                CallStatus.COMPLETE,
                result_summary=summary if isinstance(summary, str) else "",
                cost_usd=child_call_cost,
            )
        await config.trace.record(
            SubagentCompletedEvent(
                agent_id=agent_id,
                child_call_id=child_call_id or "",
                summary=summary if isinstance(summary, str) else "",
                input_tokens=transcript.input_tokens if has_usage else None,
                output_tokens=transcript.output_tokens if has_usage else None,
                cache_creation_input_tokens=transcript.cache_creation_input_tokens
                if transcript.cache_creation_input_tokens
                else None,
                cache_read_input_tokens=transcript.cache_read_input_tokens
                if transcript.cache_read_input_tokens
                else None,
                cost_usd=cost_usd,
            )
        )
        if has_usage:
            log.info(
                "Subagent %s completed (tokens: %d in / %d out, cost: $%.4f)",
                agent_id,
                transcript.input_tokens,
                transcript.output_tokens,
                cost_usd or 0.0,
            )
        else:
            log.warning(
                "Subagent %s completed but no usage data was captured "
                "(transcript=%s) — cost/tokens will show as zero",
                agent_id,
                transcript_path or "<empty>",
            )
        return SyncHookJSONOutput()

    allowed = list(config.allowed_tools) if config.allowed_tools else tool_fqnames
    if config.agents and "Agent" not in allowed:
        allowed = [*allowed, "Agent"]

    hooks: dict[HookEvent, list[HookMatcher]] = {
        "PreToolUse": [
            HookMatcher(matcher="Agent", hooks=[on_pre_tool_use]),
        ],
        "PostToolUse": [
            HookMatcher(matcher=".*", hooks=[on_post_tool_use]),
        ],
        "SubagentStart": [
            HookMatcher(matcher=".*", hooks=[on_subagent_start]),
        ],
        "SubagentStop": [
            HookMatcher(matcher=".*", hooks=[on_subagent_stop]),
        ],
    }
    for hook_type, matchers in config.extra_hooks.items():
        hooks.setdefault(hook_type, []).extend(matchers)

    options = ClaudeAgentOptions(
        system_prompt=config.system_prompt,
        mcp_servers={config.server_name: server},
        allowed_tools=allowed,
        disallowed_tools=list(config.disallowed_tools),
        agents=config.agents,
        hooks=hooks,
        max_turns=settings.sdk_agent_max_turns,
        model=settings.model,
        output_format=config.output_format,
    )

    await config.trace.record(
        AgentStartedEvent(
            system_prompt=config.system_prompt,
            user_message=config.user_prompt,
        )
    )

    last_assistant_text: list[str] = []
    all_assistant_text: list[str] = []
    structured_output: Any = None
    all_messages: list[dict] = []
    turn_counter = 0
    async with ClaudeSDKClient(options=options) as client:
        await client.query(config.user_prompt)
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                text_parts = [
                    block.text for block in message.content if isinstance(block, TextBlock)
                ]
                thinking_parts = [
                    block.thinking for block in message.content if isinstance(block, ThinkingBlock)
                ]
                tool_uses = [
                    {"tool": block.name, "input": block.input}
                    for block in message.content
                    if isinstance(block, ToolUseBlock)
                ]
                if text_parts:
                    last_assistant_text = text_parts
                    all_assistant_text.extend(text_parts)
                has_content = bool(text_parts) or bool(tool_uses)
                input_tokens: int | None = None
                output_tokens: int | None = None
                cache_creation: int | None = None
                cache_read: int | None = None
                cost_usd: float | None = None
                if message.usage:
                    input_tokens = message.usage.get("input_tokens", 0)
                    output_tokens = message.usage.get("output_tokens", 0)
                    cache_creation = message.usage.get("cache_creation_input_tokens", 0)
                    cache_read = message.usage.get("cache_read_input_tokens", 0)
                    cost_usd = (
                        compute_cost(
                            model=settings.model,
                            input_tokens=input_tokens or 0,
                            output_tokens=output_tokens or 0,
                            cache_creation_input_tokens=cache_creation or 0,
                            cache_read_input_tokens=cache_read or 0,
                        )
                        or None
                    )
                if has_content:
                    turn_counter += 1
                    response_text = "\n".join(text_parts) if text_parts else None
                    tool_call_data = [
                        {"name": block.name, "input": block.input}
                        for block in message.content
                        if isinstance(block, ToolUseBlock)
                    ]
                    try:
                        exchange_id = await config.db.save_llm_exchange(
                            call_id=config.call.id,
                            phase="sdk_agent",
                            system_prompt=config.system_prompt if turn_counter == 1 else None,
                            user_message=config.user_prompt if turn_counter == 1 else None,
                            response_text=response_text,
                            tool_calls=tool_call_data,
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                            round_num=turn_counter,
                            cache_creation_input_tokens=cache_creation,
                            cache_read_input_tokens=cache_read,
                        )
                        await config.trace.record(
                            LLMExchangeEvent(
                                exchange_id=exchange_id,
                                phase="sdk_agent",
                                round=turn_counter,
                                input_tokens=input_tokens,
                                output_tokens=output_tokens,
                                cache_creation_input_tokens=cache_creation,
                                cache_read_input_tokens=cache_read,
                                cost_usd=cost_usd,
                                has_thinking=bool(thinking_parts),
                                tool_uses=tool_uses or None,
                            )
                        )
                    except Exception as exc:
                        log.error(
                            "Failed to save SDK agent exchange for call %s: %s",
                            config.call.id[:8],
                            exc,
                        )
                        await config.trace.record(
                            WarningEvent(
                                message=(
                                    f"Failed to persist exchange "
                                    f"(round {turn_counter}): "
                                    f"{type(exc).__name__}: {exc}"
                                )
                            )
                        )
                if config.output_format:
                    all_messages.append(
                        {
                            "type": "AssistantMessage",
                            "content": [_serialize_block(b) for b in message.content],
                        }
                    )
            elif isinstance(message, ResultMessage):
                if not last_assistant_text and message.result:
                    last_assistant_text = [message.result]
                if message.structured_output is not None:
                    structured_output = message.structured_output
                if message.stop_reason == "max_turns":
                    log.warning("Agent hit max_turns limit")
                    await config.trace.record(
                        WarningEvent(message="Agent hit max_turns limit — output may be incomplete")
                    )
                if config.output_format:
                    all_messages.append(
                        {
                            "type": "ResultMessage",
                            "result": message.result,
                            "stop_reason": message.stop_reason,
                            "structured_output": message.structured_output,
                        }
                    )

    if config.output_format:
        log.info(
            "Structured output debug dump:\n%s",
            json.dumps(all_messages, indent=2, default=str),
        )
        if structured_output is None:
            structured_output = _try_extract_json(last_assistant_text)
            if structured_output is not None:
                log.info("Extracted structured output from assistant text (fallback)")

    return SdkAgentResult(
        last_assistant_text=last_assistant_text,
        all_assistant_text=all_assistant_text,
        structured_output=structured_output,
    )


def _serialize_block(block: Any) -> dict:
    """Best-effort serialization of a content block for debug logging."""
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if hasattr(block, "model_dump"):
        return block.model_dump()  # type: ignore[no-any-return]
    return {"type": type(block).__name__, "repr": repr(block)[:500]}


_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)


def _try_extract_json(text_parts: Sequence[str]) -> Any:
    """Try to parse structured JSON from assistant text.

    Checks for JSON code blocks first, then tries parsing the raw text.
    """
    full_text = "\n".join(text_parts).strip()
    if not full_text:
        return None

    block_match = _JSON_BLOCK_RE.search(full_text)
    if block_match:
        try:
            return json.loads(block_match.group(1))
        except json.JSONDecodeError:
            pass

    try:
        return json.loads(full_text)
    except json.JSONDecodeError:
        pass

    return None
