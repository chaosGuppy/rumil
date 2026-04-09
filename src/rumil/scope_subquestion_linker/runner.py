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
    `{"proposed_subquestion_ids": [...]}`. The agent does
    NOT create LINK_CHILD_QUESTION links itself; that is left to a follow-up
    review step.
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

        proposed_ids = await _validate_proposals(
            holder.result, db, scope.id, current_children_ids
        )
        log.info(
            "scope_subquestion_linker: %d proposal(s) survived validation",
            len(proposed_ids),
        )

        proposed_pages = await db.get_pages_by_ids(proposed_ids)
        proposed = [
            ProposedSubquestion(
                id=pid,
                headline=proposed_pages[pid].headline if pid in proposed_pages else "",
            )
            for pid in proposed_ids
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


async def _validate_proposals(
    result: LinkerResult,
    db: DB,
    scope_id: str,
    current_children_ids: set[str],
) -> list[str]:
    """Apply semantic validation to a schema-validated LinkerResult.

    Drops ids that point at the scope itself, are already children, are not
    questions, or are unknown. Returns deduped full UUIDs in submission order.
    """
    proposed_ids: list[str] = []
    seen: set[str] = set()
    for raw_id in result.question_ids:
        cleaned = raw_id.strip()
        resolved = await db.resolve_page_id(cleaned)
        if resolved is None:
            log.info("dropping %s: not found", cleaned)
            continue
        if resolved == scope_id:
            log.info("dropping %s: is scope itself", cleaned)
            continue
        if resolved in current_children_ids:
            log.info("dropping %s: already a child of scope", cleaned)
            continue
        if resolved in seen:
            continue
        page = await db.get_page(resolved)
        if page is None or page.page_type != PageType.QUESTION:
            log.info("dropping %s: not a question", cleaned)
            continue
        proposed_ids.append(resolved)
        seen.add(resolved)
    return proposed_ids
