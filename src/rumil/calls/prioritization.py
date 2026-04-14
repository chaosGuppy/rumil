"""Prioritization call: plan budget allocation across questions."""

import logging

from rumil.calls.common import (
    RunCallResult,
    run_single_call,
)
from rumil.calls.dispatches import estimate_dispatch_cost
from collections.abc import Sequence

from rumil.calls.dispatches import (
    DISPATCH_DEFS,
    DispatchDef,
    filter_mode_schema,
    make_mode_validator,
)
from rumil.database import DB
from rumil.available_moves import get_moves_for_call
from rumil.llm import build_system_prompt, build_user_message
from rumil.models import Call, CallStatus, CallType, MoveType
from rumil.moves.base import MoveState
from rumil.moves.registry import MOVES
from rumil.settings import get_settings

log = logging.getLogger(__name__)


async def run_prioritization_call(
    task_description: str,
    context_text: str,
    call: Call,
    db: DB,
    *,
    available_moves: list[MoveType] | None = None,
    short_id_map: dict[str, str] | None = None,
    dispatch_types: Sequence[CallType] | None = None,
    extra_dispatch_defs: Sequence[DispatchDef] | None = None,
    system_prompt_override: str | None = None,
    dispatch_budget: int | None = None,
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
        available_moves = list(get_moves_for_call(CallType.PRIORITIZATION))

    state = MoveState(call, db)
    system_prompt = system_prompt_override or build_system_prompt(
        CallType.PRIORITIZATION.value
    )

    allowed_fc_modes = get_settings().allowed_find_considerations_modes
    state._dispatch_validators.append(make_mode_validator(allowed_fc_modes))

    tools = []
    for mt in available_moves:
        tool = MOVES[mt].bind(state)
        if mt == MoveType.CREATE_SUBQUESTION:
            tool.input_schema = filter_mode_schema(tool.input_schema, allowed_fc_modes)
        tools.append(tool)
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
            state,
            short_id_map=short_id_map,
            scope_question_id=call.scope_page_id,
        )
        if ddef.call_type == CallType.FIND_CONSIDERATIONS:
            tool.input_schema = filter_mode_schema(tool.input_schema, allowed_fc_modes)
        tools.append(tool)

    user_message = build_user_message(context_text, task_description)

    agent_result = await run_single_call(
        system_prompt,
        user_message,
        tools,
        call_id=call.id,
        phase="prioritization",
        db=db,
        state=state,
    )

    if dispatch_budget is not None and state.dispatches:
        allocated = sum(estimate_dispatch_cost(d) for d in state.dispatches)
        if allocated < dispatch_budget * 0.5:
            log.warning(
                "Prioritization under-allocated: %d/%d budget dispatched, retrying once",
                allocated,
                dispatch_budget,
            )
            retry_msgs = list(agent_result.messages)
            retry_msgs.append(
                {
                    "role": "user",
                    "content": (
                        f"You have only dispatched ~{allocated} of {dispatch_budget} "
                        "available budget units. Please make your remaining dispatch "
                        "calls now."
                    ),
                }
            )
            agent_result = await run_single_call(
                system_prompt,
                tools=tools,
                call_id=call.id,
                phase="prioritization_retry",
                db=db,
                state=state,
                messages=retry_msgs,
                cache=True,
            )

    log.info(
        "run_prioritization_call complete: pages_created=%d, dispatches=%d, moves=%d",
        len(state.created_page_ids),
        len(state.dispatches),
        len(state.moves),
    )
    return RunCallResult(
        created_page_ids=state.created_page_ids,
        dispatches=state.dispatches,
        moves=state.moves,
        agent_result=agent_result,
    )
