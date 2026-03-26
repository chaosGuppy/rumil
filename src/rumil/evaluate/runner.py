"""Evaluation agent runner using the Claude Agent SDK."""

import logging

from claude_agent_sdk import (
    AgentDefinition,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookContext,
    HookInput,
    HookMatcher,
    ResultMessage,
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

    explore_tool_def = _make_explore_tool(db, trace)
    server = create_sdk_mcp_server(
        _TOOL_SERVER_NAME,
        tools=[explore_tool_def],
    )

    subagent_calls: dict[str, str] = {}

    async def on_subagent_start(
        input_data: HookInput, tool_use_id: str | None, context: HookContext
    ) -> SyncHookJSONOutput:
        agent_id: str = input_data.get("agent_id", "")  # type: ignore[call-overload]
        agent_type: str = input_data.get("agent_type", "investigator")  # type: ignore[call-overload]
        child_call = await db.create_call(
            call_type=CallType.EVALUATE,
            scope_page_id=resolved_id,
            parent_call_id=call.id,
        )
        subagent_calls[agent_id] = child_call.id
        await db.update_call_status(child_call.id, CallStatus.RUNNING)
        await trace.record(
            SubagentStartedEvent(
                agent_id=agent_id,
                agent_type=agent_type,
                child_call_id=child_call.id,
            )
        )
        log.info("Subagent %s started → child call %s", agent_id, child_call.id)
        return SyncHookJSONOutput()

    async def on_subagent_stop(
        input_data: HookInput, tool_use_id: str | None, context: HookContext
    ) -> SyncHookJSONOutput:
        agent_id: str = input_data.get("agent_id", "")  # type: ignore[call-overload]
        child_call_id = subagent_calls.get(agent_id)
        summary: str = input_data.get("result", "")  # type: ignore[call-overload]
        if len(summary) > 500:
            summary = summary[:500]
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
        allowed_tools=[_EXPLORE_TOOL_FQNAME, "Agent"],
        disallowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
        agents={
            "investigator": AgentDefinition(
                description=(
                    "Investigates a specific claim or page in the research "
                    "workspace to assess its evidential grounding."
                ),
                prompt=investigator_prompt,
                tools=[_EXPLORE_TOOL_FQNAME],
            ),
        },
        hooks={
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

    result_text = ""
    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(user_prompt)
            async for message in client.receive_response():
                if isinstance(message, ResultMessage):
                    result_text = message.result or ""

        call.review_json = {"evaluation": result_text}
        call.result_summary = result_text[:2000]
        call.status = CallStatus.COMPLETE
        await db.save_call(call)
    except Exception:
        log.exception("Evaluation agent failed")
        await db.update_call_status(call.id, CallStatus.FAILED)
        raise

    return call


def _make_explore_tool(db: DB, trace: CallTrace):
    """Create the explore_page tool definition, closing over *db* and *trace*."""

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

        await trace.record(ExplorePageEvent(page_id=page_id))

        return {"content": [{"type": "text", "text": result}]}

    return explore_page
