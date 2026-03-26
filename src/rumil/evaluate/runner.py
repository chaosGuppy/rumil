"""Evaluation agent runner using the Claude Agent SDK."""

import json
import logging
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
from claude_agent_sdk.types import SyncHookJSONOutput

from rumil.database import DB
from rumil.evaluate.explore import explore_page_impl
from rumil.evaluate.prompt import build_evaluation_prompt, build_investigator_prompt
from rumil.models import Call, CallStatus, CallType
from rumil.settings import get_settings
from rumil.tracing.broadcast import Broadcaster
from rumil.tracing.tracer import CallTrace
from rumil.tracing.trace_events import (
    ExplorePageEvent,
    SubagentCompletedEvent,
    SubagentStartedEvent,
)

log = logging.getLogger(__name__)

_TOOL_SERVER_NAME = "workspace-tools"
_EXPLORE_TOOL_FQNAME = f"mcp__{_TOOL_SERVER_NAME}__explore_page"


async def run_evaluation(
    question_id: str,
    db: DB,
    *,
    broadcaster: Broadcaster | None = None,
) -> Call:
    """Run the evaluation agent against *question_id* and return the Call record."""
    settings = get_settings()

    resolved_id = await db.resolve_page_id(question_id)
    if resolved_id is None:
        raise ValueError(f'Question "{question_id}" not found')

    question = await db.get_page(resolved_id)
    if question is None:
        raise ValueError(f'Question "{resolved_id}" not found')

    call = await db.create_call(
        call_type=CallType.EVALUATE,
        scope_page_id=resolved_id,
    )
    trace = CallTrace(call.id, db, broadcaster=broadcaster)
    await db.update_call_status(call.id, CallStatus.RUNNING)

    initial_context = await explore_page_impl(resolved_id, db)

    system_prompt = build_evaluation_prompt()
    investigator_prompt = build_investigator_prompt()

    explore_tool_def = _make_explore_tool(db)
    server = create_sdk_mcp_server(
        _TOOL_SERVER_NAME,
        tools=[explore_tool_def],
    )

    subagent_calls: dict[str, str] = {}
    subagent_traces: dict[str, CallTrace] = {}
    pending_agent_prompts: list[str] = []

    def _trace_for_agent(agent_id: str | None) -> CallTrace:
        """Return the child trace for a subagent, or the parent trace."""
        if agent_id and agent_id in subagent_traces:
            return subagent_traces[agent_id]
        return trace

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
        if tool_name == _EXPLORE_TOOL_FQNAME:
            tool_input: dict = input_data.get("tool_input", {})  # type: ignore[call-overload]
            page_id = tool_input.get("page_id", "")
            agent_id: str = input_data.get("agent_id", "")  # type: ignore[call-overload]
            headline = ""
            resolved = await db.resolve_page_id(page_id)
            if resolved:
                page = await db.get_page(resolved)
                if page:
                    page_id = resolved
                    headline = page.headline
            target_trace = _trace_for_agent(agent_id)
            await target_trace.record(
                ExplorePageEvent(page_id=page_id, page_headline=headline)
            )
        return SyncHookJSONOutput()

    async def on_subagent_start(
        input_data: HookInput, tool_use_id: str | None, context: HookContext
    ) -> SyncHookJSONOutput:
        agent_id: str = input_data.get("agent_id", "")  # type: ignore[call-overload]
        agent_type: str = input_data.get("agent_type", "investigator")  # type: ignore[call-overload]
        prompt = pending_agent_prompts.pop(0) if pending_agent_prompts else ""
        child_call = await db.create_call(
            call_type=CallType.EVALUATE,
            scope_page_id=resolved_id,
            parent_call_id=call.id,
        )
        subagent_calls[agent_id] = child_call.id
        subagent_traces[agent_id] = CallTrace(
            child_call.id, db, broadcaster=broadcaster
        )
        await db.update_call_status(child_call.id, CallStatus.RUNNING)
        await trace.record(
            SubagentStartedEvent(
                agent_id=agent_id,
                agent_type=agent_type,
                child_call_id=child_call.id,
                prompt=prompt[:2000],
            )
        )
        log.info("Subagent %s started → child call %s", agent_id, child_call.id)
        return SyncHookJSONOutput()

    async def on_subagent_stop(
        input_data: HookInput, tool_use_id: str | None, context: HookContext
    ) -> SyncHookJSONOutput:
        agent_id: str = input_data.get("agent_id", "")  # type: ignore[call-overload]
        child_call_id = subagent_calls.get(agent_id)
        summary = _read_subagent_summary(
            input_data.get("agent_transcript_path", "")  # type: ignore[call-overload]
        )
        if child_call_id:
            await db.update_call_status(
                child_call_id,
                CallStatus.COMPLETE,
                result_summary=summary,
            )
        await trace.record(
            SubagentCompletedEvent(
                agent_id=agent_id,
                child_call_id=child_call_id or "",
                summary=summary,
            )
        )
        log.info("Subagent %s completed", agent_id)
        return SyncHookJSONOutput()

    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        mcp_servers={_TOOL_SERVER_NAME: server},
        allowed_tools=[_EXPLORE_TOOL_FQNAME, "Read", "Agent"],
        disallowed_tools=["Write", "Edit", "Bash", "Glob", "Grep"],
        agents={
            "investigator": AgentDefinition(
                description=(
                    "Investigates a specific claim or page in the research "
                    "workspace to assess its evidential grounding."
                ),
                prompt=investigator_prompt,
                tools=[_EXPLORE_TOOL_FQNAME, "Read"],
            ),
        },
        hooks={
            "PreToolUse": [
                HookMatcher(matcher="Agent", hooks=[on_pre_tool_use]),
            ],
            "PostToolUse": [
                HookMatcher(matcher=_EXPLORE_TOOL_FQNAME, hooks=[on_post_tool_use]),
            ],
            "SubagentStart": [
                HookMatcher(matcher=".*", hooks=[on_subagent_start]),
            ],
            "SubagentStop": [
                HookMatcher(matcher=".*", hooks=[on_subagent_stop]),
            ],
        },
        max_turns=settings.evaluate_max_turns,
        model=settings.model,
    )

    user_prompt = (
        f"Evaluate the judgement for question ID `{resolved_id}`.\n\n"
        f"Here is the local graph around the question:\n\n{initial_context}"
    )

    last_assistant_text: list[str] = []
    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(user_prompt)
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    parts = [
                        block.text
                        for block in message.content
                        if isinstance(block, TextBlock)
                    ]
                    if parts:
                        last_assistant_text = parts
                elif isinstance(message, ResultMessage):
                    if not last_assistant_text and message.result:
                        last_assistant_text = [message.result]

        result_text = "\n\n".join(last_assistant_text)
        call.review_json = {"evaluation": result_text}
        call.result_summary = result_text[:2000]
        call.status = CallStatus.COMPLETE
        await db.save_call(call)
    except Exception:
        log.exception("Evaluation agent failed")
        await db.update_call_status(call.id, CallStatus.FAILED)
        raise

    return call


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


def _make_explore_tool(db: DB):
    """Create the explore_page tool definition, closing over *db*."""

    @tool(
        "explore_page",
        "Explore the local graph around a page. Returns the page and its "
        "neighbors at varying detail levels based on graph distance. "
        "Input a page ID (short 8-char prefix or full UUID).",
        {"page_id": str},
    )
    async def explore_page(args: dict) -> dict:
        page_id = args["page_id"]
        result = await explore_page_impl(page_id, db)
        return {"content": [{"type": "text", "text": result}]}

    return explore_page
