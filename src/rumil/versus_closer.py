"""Generic SDK-agent closer for versus runs.

Carved out of the bridge's per-task closer so additional tasks
(e.g. ``CompleteEssayTask`` in #426) can reuse the wiring without
duplicating it. The *prompts* are task-specific (the task supplies
``(system, user)`` via :meth:`VersusTask.closer_prompts`); the
*machinery* (call creation, tracing, tool wiring, settings override
on ``sdk_agent_max_turns``) is task-agnostic and lives here.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime

from rumil.database import DB
from rumil.model_config import ModelConfig
from rumil.models import Call, CallStatus, CallType
from rumil.run_eval.runner import wrap_as_mcp_tool
from rumil.sdk_agent import SdkAgentConfig, run_sdk_agent
from rumil.settings import override_settings
from rumil.tracing.broadcast import Broadcaster
from rumil.tracing.tracer import CallTrace
from rumil.workspace_exploration.explore import make_explore_subgraph_tool
from rumil.workspace_exploration.load_page import make_load_page_tool
from rumil.workspace_exploration.search import make_search_tool

log = logging.getLogger(__name__)

_DEFAULT_SERVER_NAME = "versus-judge-tools"
_DEFAULT_DISALLOWED_TOOLS: tuple[str, ...] = ("Write", "Edit", "Glob")
_DEFAULT_MAX_TURNS = 5


async def run_closer_agent(
    db: DB,
    *,
    question_id: str,
    system_prompt: str,
    user_prompt: str,
    call_type: CallType = CallType.VERSUS_JUDGE,
    server_name: str = _DEFAULT_SERVER_NAME,
    max_turns: int = _DEFAULT_MAX_TURNS,
    disallowed_tools: Sequence[str] = _DEFAULT_DISALLOWED_TOOLS,
    broadcaster: Broadcaster | None = None,
    model_config: ModelConfig | None = None,
) -> tuple[str, Call]:
    """Run a single SDK agent that emits final text and persist it as a call.

    Wires the three workspace-exploration tools, creates a call of
    ``call_type`` rooted at ``question_id``, runs the SDK agent under
    a tight ``max_turns`` budget, and returns ``(last_assistant_text,
    persisted_call)``. The caller (a :class:`VersusTask`) parses the
    text into whatever artifact it produces.
    """
    call = await db.create_call(
        call_type=call_type,
        scope_page_id=question_id,
    )
    trace = CallTrace(call.id, db, broadcaster=broadcaster)
    await db.update_call_status(call.id, CallStatus.RUNNING)

    explore_llm_tool = make_explore_subgraph_tool(db, trace, questions_only=False)
    load_page_llm_tool = make_load_page_tool(db, trace)
    search_llm_tool = make_search_tool(db, trace)
    mcp_tools = [
        wrap_as_mcp_tool(explore_llm_tool),
        wrap_as_mcp_tool(load_page_llm_tool),
        wrap_as_mcp_tool(search_llm_tool),
    ]
    allowed = [
        f"mcp__{server_name}__{t.name}"
        for t in (explore_llm_tool, load_page_llm_tool, search_llm_tool)
    ]

    config = SdkAgentConfig(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        server_name=server_name,
        mcp_tools=mcp_tools,
        call=call,
        call_type=call_type,
        scope_page_id=question_id,
        db=db,
        trace=trace,
        broadcaster=broadcaster,
        allowed_tools=allowed,
        disallowed_tools=list(disallowed_tools),
        model_config=model_config,
    )

    try:
        with override_settings(sdk_agent_max_turns=max_turns):
            result = await run_sdk_agent(config)
        # Both the versus-judge-shell system prompt and the inline user
        # prompt instruct "End your response with ... on its own line", so
        # the artifact text belongs in the FINAL turn. Scan only
        # last_assistant_text — earlier turns may mention sentinel labels
        # mid-thought ("might be A somewhat preferred") that shouldn't count.
        report_text = "\n\n".join(result.last_assistant_text)
        call.status = CallStatus.COMPLETE
        call.completed_at = datetime.now(UTC)
        call.result_summary = report_text[:500]
        if trace.total_cost_usd > 0:
            call.cost_usd = trace.total_cost_usd
        await db.save_call(call)
    except Exception:
        log.exception("versus closer failed (question=%s)", question_id)
        await db.update_call_status(call.id, CallStatus.FAILED)
        raise

    return report_text, call
