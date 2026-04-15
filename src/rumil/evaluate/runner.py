"""Evaluation agent runner using the Claude Agent SDK."""

import logging

from claude_agent_sdk import (
    AgentDefinition,
    HookContext,
    HookInput,
    HookMatcher,
)
from claude_agent_sdk.types import SyncHookJSONOutput

from rumil.database import DB
from rumil.evaluate.explore import explore_page_impl
from rumil.evaluate.prompt import build_evaluation_prompt, build_investigator_prompt
from rumil.models import Call, CallStatus, CallType
from rumil.explore_tool import make_explore_tool
from rumil.sdk_agent import (
    SdkAgentConfig,
    extract_response_text,
    run_sdk_agent,
)
from rumil.tracing.broadcast import Broadcaster
from rumil.tracing.tracer import CallTrace
from rumil.tracing.trace_events import (
    EvaluationCompleteEvent,
    ExplorePageEvent,
)

log = logging.getLogger(__name__)

_TOOL_SERVER_NAME = "workspace-tools"
_EXPLORE_TOOL_FQNAME = f"mcp__{_TOOL_SERVER_NAME}__explore_page"


async def run_evaluation(
    question_id: str,
    db: DB,
    *,
    eval_type: str = "default",
    broadcaster: Broadcaster | None = None,
) -> Call:
    """Run the evaluation agent against *question_id* and return the Call record."""
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
    system_prompt = build_evaluation_prompt(eval_type)
    investigator_prompt = build_investigator_prompt(eval_type)
    explore_tool = make_explore_tool(db)

    user_prompt = (
        f"Evaluate the judgement for question ID `{resolved_id}`.\n\n"
        f"Here is the local graph around the question:\n\n{initial_context}"
    )

    async def on_explore_page(
        input_data: HookInput, tool_use_id: str | None, context: HookContext
    ) -> SyncHookJSONOutput:
        tool_input: dict = input_data.get("tool_input", {})  # type: ignore[call-overload]
        page_id = tool_input.get("page_id", "")
        headline = ""
        resolved = await db.resolve_page_id(page_id)
        if resolved:
            page = await db.get_page(resolved)
            if page:
                page_id = resolved
                headline = page.headline
        response = extract_response_text(input_data)
        await trace.record(
            ExplorePageEvent(
                page_id=page_id,
                page_headline=headline,
                response=response,
            )
        )
        return SyncHookJSONOutput()

    config = SdkAgentConfig(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        server_name=_TOOL_SERVER_NAME,
        mcp_tools=[explore_tool],
        call=call,
        call_type=CallType.EVALUATE,
        scope_page_id=resolved_id,
        db=db,
        trace=trace,
        broadcaster=broadcaster,
        allowed_tools=[_EXPLORE_TOOL_FQNAME, "Read", "Grep", "Bash"],
        disallowed_tools=(),
        agents={
            "investigator": AgentDefinition(
                description=(
                    "Investigates a specific claim or page in the research "
                    "workspace to assess its evidential grounding."
                ),
                prompt=investigator_prompt,
                tools=[_EXPLORE_TOOL_FQNAME, "Read", "Grep", "Bash"],
            ),
        },
        extra_hooks={
            "PostToolUse": [
                HookMatcher(matcher=_EXPLORE_TOOL_FQNAME, hooks=[on_explore_page]),
            ],
        },
    )

    try:
        result = await run_sdk_agent(config)
        result_text = "\n\n".join(result.all_assistant_text)
        await trace.record_strict(EvaluationCompleteEvent(evaluation=result_text))
        call.review_json = {"evaluation": result_text}
        call.result_summary = result_text
        call.status = CallStatus.COMPLETE
        await db.save_call(call)
    except Exception:
        log.exception("Evaluation agent failed")
        await db.update_call_status(call.id, CallStatus.FAILED)
        raise

    return call
