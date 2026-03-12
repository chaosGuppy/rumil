"""Shared utilities for call types."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from differential.context import format_page
from differential.database import DB
from differential.settings import get_settings
from differential.llm import (
    AgentResult,
    build_system_prompt,
    build_user_message,
    structured_call,
    agent_loop,
    single_call_with_tools,
)
from differential.models import (
    Call,
    CallStatus,
    CallType,
    Dispatch,
    Move,
    MoveType,
)
from differential.calls.dispatches import DISPATCH_DEFS
from differential.moves.base import MoveState
from differential.moves.load_page import LoadPagePayload
from differential.moves.registry import MOVES

log = logging.getLogger(__name__)

from differential.trace_events import (
    LLMExchangeEvent,
    MovesExecutedEvent,
    PageRef,
    WarningEvent,
)
from differential.tracer import CallTrace

PAGE_CREATING_MOVES = {
    MoveType.CREATE_CLAIM, MoveType.CREATE_QUESTION, MoveType.CREATE_JUDGEMENT,
    MoveType.CREATE_CONCEPT, MoveType.CREATE_WIKI_PAGE,
    MoveType.PROPOSE_HYPOTHESIS, MoveType.SUPERSEDE_PAGE,
}


PHASE1_TASK = (
    'Perform your preliminary analysis now. Review the workspace map above and '
    'load all pages you expect to want during your main task — err on the side of '
    'loading more rather than fewer. This is your only chance to gather context '
    'before the main task begins; load everything relevant in one go. '
    'The main task description will follow in the next turn.'
)


class PageRating(BaseModel):
    page_id: str = Field(description="Short ID of the rated page")
    score: int = Field(
        description="-1 = confusing, 0 = no help, 1 = helpful, 2 = very helpful"
    )
    note: str = Field("", description="One sentence on why")


class ReviewResponse(BaseModel):
    remaining_fruit: int = Field(
        description=(
            "0-10 integer: how much useful work remains on this scope. "
            "0 = nothing more to add; 1-2 = close to exhausted; "
            "3-4 = most angles covered; 5-6 = diminishing but real returns; "
            "7-8 = substantial work remains; 9-10 = barely started"
        )
    )
    confidence_in_output: float = Field(
        description="0-5 confidence in the work just done"
    )
    context_was_adequate: bool = Field(
        description="Whether the context provided was sufficient"
    )
    what_was_missing: str = Field(
        "", description="What additional context would have helped"
    )
    tensions_noticed: str = Field(
        "", description="Any conflicts or inconsistencies noticed"
    )
    self_assessment: str = Field("", description="1-2 sentences on how this call went")
    suggested_next_steps: str = Field("", description="What should happen next")
    page_ratings: list[PageRating] = Field(
        default_factory=list,
        description="Ratings for pages that were loaded into context",
    )


@dataclass
class RunCallResult:
    """Result of a run_call invocation."""

    created_page_ids: list[str] = field(default_factory=list)
    dispatches: list[Dispatch] = field(default_factory=list)
    moves: list[Move] = field(default_factory=list)
    phase1_page_ids: list[str] = field(default_factory=list)
    agent_result: AgentResult = field(default_factory=AgentResult)


async def _save_exchanges(
    agent_result: AgentResult,
    call_id: str,
    phase: str,
    db: DB,
    trace: "CallTrace | None" = None,
    moves: list[Move] | None = None,
    created_page_ids: list[str] | None = None,
) -> None:
    """Save LLM exchange records and interleave moves per round."""
    move_tool_names = {md.name for md in MOVES.values()}
    move_idx = 0
    create_idx = 0
    for rr in agent_result.rounds:
        tc_data = [
            {"name": tc.name, "input": tc.input, "result": tc.result[:500]}
            for tc in rr.tool_calls
        ]
        exchange_id = await db.save_llm_exchange(
            call_id=call_id,
            phase=phase,
            round_num=rr.round,
            system_prompt=agent_result.system_prompt,
            user_message=agent_result.user_message if rr.round == 0 else None,
            response_text=rr.response_text,
            tool_calls=tc_data,
            input_tokens=rr.input_tokens,
            output_tokens=rr.output_tokens,
            error=rr.error,
            duration_ms=rr.duration_ms or None,
        )
        if trace:
            await trace.record(LLMExchangeEvent(
                exchange_id=exchange_id,
                phase=phase,
                round=rr.round,
                input_tokens=rr.input_tokens,
                output_tokens=rr.output_tokens,
                duration_ms=rr.duration_ms or None,
            ))
        if trace and moves:
            round_move_count = sum(
                1 for tc in rr.tool_calls if tc.name in move_tool_names
            )
            if round_move_count > 0:
                round_moves = moves[move_idx:move_idx + round_move_count]
                move_idx += round_move_count
                round_created: list[str] = []
                if created_page_ids:
                    for m in round_moves:
                        if (m.move_type in PAGE_CREATING_MOVES
                                and create_idx < len(created_page_ids)):
                            round_created.append(created_page_ids[create_idx])
                            create_idx += 1
                await trace.record(await moves_to_trace_event(round_moves, round_created, db))
    for w in agent_result.warnings:
        if trace:
            await trace.record(WarningEvent(message=w))


async def _format_loaded_pages(page_ids: list[str], db: DB) -> str:
    """Format loaded pages as context text for phase 2."""
    parts = []
    for pid in page_ids:
        page = await db.get_page(pid)
        if page:
            parts.append(f"### Page `{pid[:8]}`\n\n{await format_page(page, db=db)}")
    return "\n\n---\n\n".join(parts)


async def _run_phase1(
    system_prompt: str,
    context_text: str,
    state: MoveState,
    db: DB,
    trace: "CallTrace | None" = None,
) -> list[str]:
    """Preliminary page loading via single LLM call with load_page tool.

    Returns resolved full page IDs. Free (not counted against budget).
    """
    log.debug("Phase 1 starting: context_len=%d", len(context_text))
    try:
        phase1_msg = build_user_message(context_text, PHASE1_TASK)
        load_page_tool = MOVES[MoveType.LOAD_PAGE].bind(state)
        moves_before = len(state.moves)
        result = await single_call_with_tools(
            system_prompt=system_prompt,
            user_message=phase1_msg,
            tools=[load_page_tool],
            max_tokens=2048,
        )
        phase1_moves = state.moves[moves_before:]
        if trace:
            await _save_exchanges(
                result, trace.call_id, "initial_page_loads", db, trace,
                moves=phase1_moves,
            )
        loaded_ids = []
        for tc in result.tool_calls:
            if tc.name == "load_page":
                full_id = await state.db.resolve_page_id(tc.input.get("page_id", ""))
                if full_id:
                    loaded_ids.append(full_id)
        if loaded_ids:
            labels = [await state.db.page_label(pid) for pid in loaded_ids]
            log.info("Phase 1 loaded %d pages: %s", len(loaded_ids), labels)
        else:
            log.debug("Phase 1 completed with no pages loaded")
        return loaded_ids
    except Exception as e:
        log.warning("Phase 1 skipped due to error: %s", e, exc_info=True)
        return []


async def run_call(
    call_type: CallType,
    task_description: str,
    context_text: str,
    call: Call,
    db: DB,
    *,
    available_moves: list[MoveType] | None = None,
    max_tokens: int = 4096,
    max_rounds: int | None = None,
    subtree_ids: set[str] | None = None,
    short_id_map: dict[str, str] | None = None,
    trace: "CallTrace | None" = None,
) -> RunCallResult:
    """Run a workspace call with tool use.

    For non-prioritization calls, runs a preliminary phase where the LLM can
    load pages before starting its main work. Moves are executed immediately
    when the LLM calls them. Returns a RunCallResult with created page IDs,
    dispatches, and the raw agent result.
    """

    if max_rounds is None:
        max_rounds = 1 if get_settings().is_smoke_test else 3

    log.info(
        "run_call: type=%s, call=%s, scope=%s",
        call_type.value, call.id[:8],
        call.scope_page_id[:8] if call.scope_page_id else None,
    )

    if available_moves is None:
        available_moves = list(MoveType)

    state = MoveState(call, db)
    system_prompt = build_system_prompt(call_type.value)

    phase1_ids: list[str] = []
    if call_type != CallType.PRIORITIZATION:
        phase1_ids = await _run_phase1(system_prompt, context_text, state, db, trace=trace)
        if phase1_ids:
            extra_text = await _format_loaded_pages(phase1_ids, db)
            context_text = context_text + "\n\n## Loaded Pages\n\n" + extra_text

    tools = [MOVES[mt].bind(state) for mt in available_moves]
    if call_type == CallType.PRIORITIZATION:
        for ddef in DISPATCH_DEFS.values():
            tools.append(ddef.bind(state, subtree_ids, short_id_map))

    user_message = build_user_message(context_text, task_description)

    phase = "prioritization" if call_type == CallType.PRIORITIZATION else "inner_loop"
    moves_before = len(state.moves)
    created_before = len(state.created_page_ids)
    if call_type == CallType.PRIORITIZATION:
        agent_result = await single_call_with_tools(
            system_prompt,
            user_message,
            tools,
            max_tokens=max_tokens,
        )
    else:
        agent_result = await agent_loop(
            system_prompt,
            user_message,
            tools,
            max_tokens=max_tokens,
            max_rounds=max_rounds,
        )
    phase_moves = state.moves[moves_before:]
    phase_created = state.created_page_ids[created_before:]
    if trace:
        await _save_exchanges(
            agent_result, call.id, phase, db, trace,
            moves=phase_moves, created_page_ids=phase_created,
        )

    log.info(
        "run_call complete: type=%s, pages_created=%d, dispatches=%d, moves=%d",
        call_type.value, len(state.created_page_ids),
        len(state.dispatches), len(state.moves),
    )
    return RunCallResult(
        created_page_ids=state.created_page_ids,
        dispatches=state.dispatches,
        moves=state.moves,
        phase1_page_ids=phase1_ids,
        agent_result=agent_result,
    )


async def extract_loaded_page_ids(result: RunCallResult, db: DB) -> list[str]:
    """Extract full page IDs for LOAD_PAGE moves from phase 2 only."""
    phase1_set = set(result.phase1_page_ids)
    loaded = []
    for m in result.moves:
        if m.move_type == MoveType.LOAD_PAGE:
            assert isinstance(m.payload, LoadPagePayload)
            full_id = await db.resolve_page_id(m.payload.page_id)
            if full_id and full_id not in phase1_set:
                loaded.append(full_id)
    return loaded


async def resolve_page_refs(page_ids: list[str], db: DB) -> list[PageRef]:
    """Resolve a list of page IDs to PageRef objects with summaries."""
    refs = []
    for pid in page_ids:
        page = await db.get_page(pid)
        summary = page.summary if page else ""
        refs.append(PageRef(id=pid, summary=summary))
    return refs


async def moves_to_trace_event(
    moves: list[Move],
    created_page_ids: list[str],
    db: DB,
) -> MovesExecutedEvent:
    """Build a typed MovesExecutedEvent from a list of moves."""
    refs = await resolve_page_refs(created_page_ids, db)
    return MovesExecutedEvent(
        moves=[
            {
                "type": m.move_type.value,
                **m.payload.model_dump(exclude_none=True, exclude_defaults=True),
            }
            for m in moves
        ],
        created_page_ids=refs,
    )


REVIEW_SYSTEM_PROMPT = (
    "You are a research assistant completing a closing review of a call you just made "
    "in a collaborative research workspace. Be honest and specific in your self-assessment."
)


async def log_page_ratings(review: dict, db: DB) -> None:
    ratings = review.get("page_ratings", [])
    if not ratings:
        return
    score_labels = {-1: "confusing", 0: "no help", 1: "helpful", 2: "very helpful"}
    for r in ratings:
        pid = r.get("page_id", "?")
        resolved = await db.resolve_page_id(pid) if pid != "?" else None
        page_label = await db.page_label(resolved or pid) if resolved else f"[{pid}]"
        score = r.get("score", "?")
        note = r.get("note", "")
        label = score_labels.get(score, str(score))
        log.info("Page rating: %s [%s]: %s", page_label, label, note)


async def complete_call(
    call: Call, db: DB, summary: str, trace: CallTrace | None = None
) -> None:
    call.status = CallStatus.COMPLETE
    call.completed_at = datetime.now(UTC)
    call.result_summary = summary
    await db.save_call(call)
    if trace:
        await trace.save()


async def run_closing_review(
    call: Call,
    main_output: str,
    context_text: str,
    loaded_page_ids: list[str] | None = None,
    db: DB | None = None,
) -> dict | None:
    """Run the closing review as a separate call. Free (not counted against budget)."""
    page_rating_note = ""
    if loaded_page_ids and db:
        page_lines = []
        for pid in loaded_page_ids:
            page = await db.get_page(pid)
            if page:
                page_lines.append(f'  - `{pid[:8]}`: "{page.summary[:120]}"')
        if page_lines:
            page_rating_note = (
                "\n\nThe following pages were loaded into your context beyond the base "
                "working context:\n"
                + "\n".join(page_lines)
                + "\n\nPlease include a rating for each in your page_ratings. "
                "Scores: -1 = actively confusing, 0 = didn't help, "
                "1 = helped, 2 = extremely helpful."
            )

    review_task = (
        f"You have just completed a {call.call_type.value} call.\n\n"
        f"Here is your output from that call:\n{main_output}\n\n"
        "Please review your work and provide your assessment."
        f"{page_rating_note}"
    )

    log.debug(
        "Closing review starting: call=%s, type=%s, loaded_pages=%d",
        call.id[:8], call.call_type.value, len(loaded_page_ids or []),
    )
    try:
        user_message = build_user_message(context_text, review_task)
        review = await structured_call(
            system_prompt=REVIEW_SYSTEM_PROMPT,
            user_message=user_message,
            response_model=ReviewResponse,
            max_tokens=2048,
        )
        if review:
            log.info(
                "Closing review complete: call=%s, fruit=%s, confidence=%s",
                call.id[:8],
                review.get("remaining_fruit"),
                review.get("confidence_in_output"),
            )
            if db:
                for r in review.get("page_ratings", []):
                    pid = await db.resolve_page_id(r.get("page_id", ""))
                    score = r.get("score")
                    if pid and isinstance(score, int):
                        await db.save_page_rating(pid, call.id, score, r.get("note", ""))
        else:
            log.warning("Closing review returned None for call=%s", call.id[:8])
        return review
    except Exception as e:
        log.error(
            "Closing review failed for call=%s: %s", call.id[:8], e, exc_info=True,
        )
        return None


def format_moves_for_review(moves: list[Move]) -> str:
    """Format moves as readable text for closing review context."""
    if not moves:
        return "(no moves)"
    parts = []
    for m in moves:
        summary = getattr(m.payload, "summary", "")
        if summary:
            parts.append(f"- {m.move_type.value}: {summary}")
        else:
            parts.append(f"- {m.move_type.value}")
    return "\n".join(parts)
