"""Shared infrastructure for running Claude Agent SDK agents with tracing."""

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from pathlib import Path

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
    create_sdk_mcp_server,
    tool,
)
from claude_agent_sdk.types import HookEvent, SyncHookJSONOutput

from rumil.database import DB
from rumil.models import Call, CallStatus, CallType
from rumil.settings import get_settings
from rumil.tracing.broadcast import Broadcaster
from rumil.tracing.tracer import CallTrace
from rumil.tracing.trace_events import (
    AgentStartedEvent,
    SubagentCompletedEvent,
    SubagentStartedEvent,
    ToolCallEvent,
    WarningEvent,
)

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
    structured_output: Any = None


def _read_subagent_summary(transcript_path: str, max_len: int = 500) -> str:
    """Extract the last assistant text from a subagent transcript file."""
    if not transcript_path:
        return ""
    try:
        lines = Path(transcript_path).read_text().splitlines()
        last_text = ""
        for line in lines:
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("type") == "assistant":
                for block in msg.get("message", {}).get("content", []):
                    if block.get("type") == "text":
                        last_text = block["text"]
        if len(last_text) > max_len:
            last_text = last_text[:max_len]
        return last_text
    except Exception:
        log.debug("Could not read subagent transcript: %s", transcript_path)
        return ""


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

    def _trace_for_agent(agent_id: str | None) -> CallTrace:
        """Return the child trace for a subagent, or the parent trace."""
        if agent_id and agent_id in subagent_traces:
            return subagent_traces[agent_id]
        return config.trace

    async def on_pre_tool_use(
        input_data: HookInput, tool_use_id: str | None, context: HookContext
    ) -> SyncHookJSONOutput:
        tool_name: str = input_data.get("tool_name", "")  # type: ignore[call-overload]
        if tool_name == "Agent":
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
        summary = input_data.get("agent_result", "")  # type: ignore[call-overload]
        if not isinstance(summary, str) or not summary:
            summary = _read_subagent_summary(
                input_data.get("agent_transcript_path", "")  # type: ignore[call-overload]
            )
        if not isinstance(summary, str):
            summary = ""
        if child_call_id:
            await config.db.update_call_status(
                child_call_id,
                CallStatus.COMPLETE,
                result_summary=summary if isinstance(summary, str) else "",
            )
        await config.trace.record(
            SubagentCompletedEvent(
                agent_id=agent_id,
                child_call_id=child_call_id or "",
                summary=summary if isinstance(summary, str) else "",
            )
        )
        log.info("Subagent %s completed", agent_id)
        return SyncHookJSONOutput()

    allowed = list(config.allowed_tools) if config.allowed_tools else tool_fqnames
    if config.agents and "Agent" not in allowed:
        allowed = allowed + ["Agent"]

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
    structured_output: Any = None
    all_messages: list[dict] = []
    async with ClaudeSDKClient(options=options) as client:
        await client.query(config.user_prompt)
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                parts = [
                    block.text
                    for block in message.content
                    if isinstance(block, TextBlock)
                ]
                if parts:
                    last_assistant_text = parts
                if config.output_format:
                    all_messages.append({
                        "type": "AssistantMessage",
                        "content": [
                            _serialize_block(b) for b in message.content
                        ],
                    })
            elif isinstance(message, ResultMessage):
                if not last_assistant_text and message.result:
                    last_assistant_text = [message.result]
                if message.structured_output is not None:
                    structured_output = message.structured_output
                if message.stop_reason == "max_turns":
                    log.warning("Agent hit max_turns limit")
                    await config.trace.record(
                        WarningEvent(
                            message="Agent hit max_turns limit — "
                            "output may be incomplete"
                        )
                    )
                if config.output_format:
                    all_messages.append({
                        "type": "ResultMessage",
                        "result": message.result,
                        "stop_reason": message.stop_reason,
                        "structured_output": message.structured_output,
                    })

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
