"""Prioritization call: plan budget allocation across questions."""

import logging

from rumil.calls.common import (
    RunCallResult,
    mark_call_completed,
    run_single_call,
)
from collections.abc import Sequence

from rumil.calls.dispatches import DISPATCH_DEFS, DispatchDef, filter_mode_schema, make_mode_validator
from rumil.context import build_prioritization_context, collect_subtree_ids
from rumil.database import DB
from rumil.page_graph import PageGraph
from rumil.llm import build_system_prompt, build_user_message
from rumil.models import Call, CallStatus, CallType, MoveType
from rumil.moves.base import MoveState
from rumil.moves.create_question import PRIORITIZATION_MOVE
from rumil.moves.registry import MOVES
from rumil.settings import get_settings
from rumil.tracing.trace_events import ContextBuiltEvent, DispatchTraceItem, DispatchesPlannedEvent
from rumil.tracing.tracer import CallTrace

log = logging.getLogger(__name__)


async def run_prioritization_call(
    task_description: str,
    context_text: str,
    call: Call,
    db: DB,
    *,
    available_moves: list[MoveType] | None = None,
    subtree_ids: set[str] | None = None,
    short_id_map: dict[str, str] | None = None,
    trace: CallTrace | None = None,
    dispatch_types: Sequence[CallType] | None = None,
    extra_dispatch_defs: Sequence[DispatchDef] | None = None,
    system_prompt_override: str | None = None,
) -> RunCallResult:
    """Run a prioritization call with tool use (single LLM round).

    Uses the prioritization-specific create_subquestion tool variant and
    dispatch tools. No phase-1 page loading.
    """
    log.info(
        "run_prioritization_call: call=%s, scope=%s",
        call.id[:8],
        call.scope_page_id[:8] if call.scope_page_id else None,
    )
    await db.update_call_status(call.id, CallStatus.RUNNING)

    if available_moves is None:
        available_moves = list(MoveType)

    state = MoveState(call, db)
    system_prompt = system_prompt_override or build_system_prompt(CallType.PRIORITIZATION.value)

    allowed_fc_modes = get_settings().allowed_find_considerations_modes
    state._dispatch_validators.append(make_mode_validator(allowed_fc_modes))

    tools = []
    for mt in available_moves:
        if mt == MoveType.CREATE_QUESTION:
            tool = PRIORITIZATION_MOVE.bind(state)
            tool.input_schema = filter_mode_schema(tool.input_schema, allowed_fc_modes)
            tools.append(tool)
        else:
            tools.append(MOVES[mt].bind(state))
    if dispatch_types is not None:
        selected_defs = [
            DISPATCH_DEFS[ct] for ct in dispatch_types if ct in DISPATCH_DEFS
        ]
    else:
        selected_defs = list(DISPATCH_DEFS.values())
    if extra_dispatch_defs:
        selected_defs.extend(extra_dispatch_defs)
    for ddef in selected_defs:
        tool = ddef.bind(
            state, subtree_ids, short_id_map,
            scope_question_id=call.scope_page_id,
        )
        if ddef.call_type == CallType.FIND_CONSIDERATIONS:
            tool.input_schema = filter_mode_schema(tool.input_schema, allowed_fc_modes)
        tools.append(tool)

    user_message = build_user_message(context_text, task_description)

    agent_result = await run_single_call(
        system_prompt, user_message, tools,
        call_id=call.id,
        phase="prioritization",
        db=db,
        state=state,
        trace=trace,
    )

    log.info(
        "run_prioritization_call complete: pages_created=%d, dispatches=%d, moves=%d",
        len(state.created_page_ids),
        len(state.dispatches), len(state.moves),
    )
    return RunCallResult(
        created_page_ids=state.created_page_ids,
        dispatches=state.dispatches,
        moves=state.moves,
        agent_result=agent_result,
    )


async def run_prioritization(
    scope_question_id: str,
    call: Call,
    budget: int,
    db: DB,
    broadcaster=None,
) -> dict:
    """Run a Prioritization call.

    Returns a summary dict including the list of dispatches and trace.
    """
    trace = CallTrace(call.id, db, broadcaster=broadcaster)
    log.info(
        "Prioritization starting: call=%s, question=%s, budget=%d",
        call.id[:8], scope_question_id[:8], budget,
    )
    graph = await PageGraph.load(db)
    context_text, short_id_map = await build_prioritization_context(
        db, scope_question_id=scope_question_id, graph=graph,
    )
    subtree_ids = await collect_subtree_ids(scope_question_id, db, graph=graph)
    await trace.record(ContextBuiltEvent(budget=budget))

    task = (
        f"You have a budget of **{budget} research calls** to allocate on this question.\n\n"
        f"Scope question ID: `{scope_question_id}`\n\n"
        "Review the current state of the workspace above and decide how to spend the budget. "
        "You may also create subquestions (using create_question + link_child_question) to "
        "decompose the scope question before dispatching research on them. "
        "Dispatch is restricted to questions within this scope subtree. "
        "Use the dispatch tool to allocate calls."
    )

    result = await run_prioritization_call(
        task,
        context_text,
        call,
        db,
        subtree_ids=subtree_ids,
        short_id_map=short_id_map,
        trace=trace,
    )

    await trace.record(DispatchesPlannedEvent(
        dispatches=[
            DispatchTraceItem(
                call_type=d.call_type.value,
                **d.payload.model_dump(exclude_defaults=True),
            )
            for d in result.dispatches
        ],
    ))

    summary = {
        "dispatches": result.dispatches,
        "moves_created": len(result.moves),
        "trace": trace,
    }

    log.info(
        "Prioritization complete: call=%s, dispatches=%d, moves=%d",
        call.id[:8], len(result.dispatches), len(result.moves),
    )
    await mark_call_completed(
        call,
        db,
        f"Prioritization complete. Planned {len(result.dispatches)} dispatches.",
    )
    return summary
