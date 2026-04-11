"""Runner for the scope-subquestion linker agent."""

import logging

from rumil.calls.common import run_agent_loop
from rumil.database import DB
from rumil.models import Call, CallStatus, CallType, PageType
from rumil.moves.base import MoveState
from rumil.scope_subquestion_linker.prompt import build_linker_prompt
from rumil.scope_subquestion_linker.seed_selection import select_seed_questions
from rumil.scope_subquestion_linker.subgraph import render_question_subgraph
from rumil.scope_subquestion_linker.tool import (
    LinkerResult,
    SubmitHolder,
    make_render_subgraph_tool,
    make_submit_tool,
)
from rumil.scope_subquestion_linker.validation import validate_proposals
from rumil.settings import get_settings
from rumil.tracing.broadcast import Broadcaster
from rumil.tracing.trace_events import (
    LinkSubquestionsCompleteEvent,
    ProposedSubquestion,
)
from rumil.tracing.tracer import CallTrace, set_trace

log = logging.getLogger(__name__)


async def run_scope_subquestion_linker(
    scope_question_id: str,
    db: DB,
    *,
    max_rounds: int | None = None,
    broadcaster: Broadcaster | None = None,
    parent_call_id: str | None = None,
) -> Call:
    """Explore the workspace looking for subquestions to link to a scope question.

    Returns the persisted Call. The call's `review_json` will contain
    `{"proposed_subquestion_ids": [...]}`. This runner only proposes
    subquestions; it does not create CHILD_QUESTION links itself.
    `ExperimentalOrchestrator` is currently the sole caller and
    materializes the proposed links inline via `link_pages` immediately
    after the runner returns.
    """
    settings = get_settings()

    resolved_id = await db.resolve_page_id(scope_question_id)
    if resolved_id is None:
        raise ValueError(f'Scope question "{scope_question_id}" not found')
    scope = await db.get_page(resolved_id)
    if scope is None:
        raise ValueError(f'Scope question "{resolved_id}" not found')
    if scope.page_type != PageType.QUESTION:
        raise ValueError(
            f"Page `{resolved_id[:8]}` is not a question (type={scope.page_type.value})"
        )

    call = await db.create_call(
        call_type=CallType.LINK_SUBQUESTIONS,
        scope_page_id=resolved_id,
        parent_call_id=parent_call_id,
    )
    trace = CallTrace(call.id, db, broadcaster=broadcaster)
    set_trace(trace)
    await db.update_call_status(call.id, CallStatus.RUNNING)

    effective_max_rounds = max_rounds or settings.scope_subquestion_linker_max_rounds
    log.info(
        "scope_subquestion_linker: starting call %s for scope `%s` -- %s (max_rounds=%d)",
        call.id,
        resolved_id[:8],
        scope.headline,
        effective_max_rounds,
    )

    try:
        seeds = await select_seed_questions(
            scope, db, limit=settings.scope_subquestion_linker_seed_limit
        )
        log.info("scope_subquestion_linker: selected %d seed question(s)", len(seeds))
        seed_blocks: list[str] = []
        for seed in seeds:
            sub = await render_question_subgraph(
                seed.id,
                db,
                max_pages=settings.scope_subquestion_linker_subgraph_max_pages,
                exclude_ids={scope.id},
            )
            if sub:
                seed_blocks.append(sub)
        seed_block = "\n\n".join(seed_blocks)

        current_children = await db.get_child_questions(scope.id)
        current_children_ids: set[str] = {c.id for c in current_children}
        log.info(
            "scope_subquestion_linker: scope already has %d linked subquestion(s)",
            len(current_children),
        )
        if current_children:
            current_children_block = "\n".join(
                f"- `{c.id[:8]}` -- {c.headline}" for c in current_children
            )
        else:
            current_children_block = ""

        system_prompt = build_linker_prompt(effective_max_rounds)
        user_message = (
            f"Find subquestions to link to scope `{scope.id[:8]}`: {scope.headline}\n\n"
            "## Scope question\n\n"
            f"`{scope.id[:8]}` -- {scope.headline}\n\n"
            f"{scope.content or scope.abstract}\n\n"
            "## Currently-linked subquestions of the scope\n\n"
            f"{current_children_block or '(none)'}\n\n"
            "## Seed subgraphs (most relevant top-level questions)\n\n"
            f"{seed_block or '(none)'}\n"
        )

        state = MoveState(call, db)
        holder = SubmitHolder()
        tools = [
            make_render_subgraph_tool(db, trace),
            make_submit_tool(holder),
        ]

        log.info("scope_subquestion_linker: entering agent loop")
        await run_agent_loop(
            system_prompt,
            user_message,
            tools,
            call_id=call.id,
            db=db,
            state=state,
            max_rounds=effective_max_rounds,
            cache=True,
        )

        if holder.result is not None:
            log.info(
                "scope_subquestion_linker: agent submitted %d raw question id(s)",
                len(holder.result.question_ids),
            )
        if holder.result is None:
            log.warning(
                "scope_subquestion_linker: agent did not call submit_linked_subquestions"
            )
            holder.result = LinkerResult(question_ids=[])

        proposed_pages = await validate_proposals(
            holder.result, db, scope.id, current_children_ids
        )
        proposed_ids = [p.id for p in proposed_pages]
        log.info(
            "scope_subquestion_linker: %d proposal(s) survived validation",
            len(proposed_ids),
        )

        proposed = [
            ProposedSubquestion(id=p.id, headline=p.headline) for p in proposed_pages
        ]
        await trace.record(LinkSubquestionsCompleteEvent(proposed=proposed))

        call.review_json = {
            "proposed_subquestion_ids": proposed_ids,
        }
        call.result_summary = f"{len(proposed_ids)} proposed subquestion(s)"
        call.status = CallStatus.COMPLETE
        await db.save_call(call)
    except Exception:
        log.exception("Scope subquestion linker failed")
        await db.update_call_status(call.id, CallStatus.FAILED)
        raise

    return call
