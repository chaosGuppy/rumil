"""Shared utilities for call types."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

import anthropic
from anthropic.types import TextBlock, ToolUseBlock
from pydantic import BaseModel, Field

from differential.context import format_page
from differential.database import DB
from differential.settings import get_settings
from differential.llm import (
    AgentResult,
    LLMExchangeMetadata,
    RoundRecord,
    Tool,
    ToolCall,
    build_system_prompt,
    build_user_message,
    call_api,
    structured_call,
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
from differential.trace_events import (
    MoveTraceItem,
    MovesExecutedEvent,
    PageRef,
    WarningEvent,
)
from differential.tracer import CallTrace

log = logging.getLogger(__name__)

PAGE_ID_FIELDS: dict[MoveType, list[str]] = {
    MoveType.LOAD_PAGE: ["page_id"],
    MoveType.LINK_CONSIDERATION: ["claim_id", "question_id"],
    MoveType.LINK_CHILD_QUESTION: ["child_id", "parent_id"],
    MoveType.LINK_RELATED: ["from_page_id", "to_page_id"],
    MoveType.SUPERSEDE_PAGE: ["old_page_id"],
    MoveType.FLAG_FUNNINESS: ["page_id"],
    MoveType.REPORT_DUPLICATE: ["page_id_a", "page_id_b"],
    MoveType.PROPOSE_HYPOTHESIS: ["parent_question_id"],
}


PHASE1_TASK = (
    'Perform your preliminary analysis now. Review the workspace map above and '
    'load all pages you expect to want during your main task — err on the side of '
    'loading more rather than fewer. This is your only chance to gather context '
    'before the main task begins; load everything relevant in one go. '
    'The main task description will follow in the next turn.'
)


async def _execute_tool_uses(
    tool_uses: list[ToolUseBlock],
    tool_fns: dict,
) -> tuple[list[ToolCall], list[dict]]:
    """Execute tool calls and build tool_result messages."""
    tool_calls: list[ToolCall] = []
    tool_results: list[dict] = []
    for tu in tool_uses:
        fn = tool_fns.get(tu.name)
        if fn is None:
            result_str = f"Unknown tool: {tu.name}"
            log.warning("Unknown tool called by LLM: %s", tu.name)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": result_str,
                "is_error": True,
            })
        else:
            try:
                result_str = await fn(tu.input)
            except Exception as e:
                log.error(
                    "Tool %s raised an exception: %s", tu.name, e, exc_info=True,
                )
                result_str = f"Error: {e}"
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": result_str,
                    "is_error": True,
                })
            else:
                log.debug(
                    "Tool %s returned: %s",
                    tu.name, result_str[:200] if result_str else "(empty)",
                )
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": result_str,
                })
        tool_calls.append(ToolCall(name=tu.name, input=tu.input, result=result_str))
    return tool_calls, tool_results


async def _record_round_moves(
    rr: RoundRecord,
    *,
    trace: CallTrace,
    state: MoveState,
    move_cursor: int,
    move_tool_names: set[str],
    db: DB,
) -> int:
    """Record move trace events for tools executed in this round. Returns updated move_cursor."""
    round_move_count = sum(
        1 for tc in rr.tool_calls if tc.name in move_tool_names
    )
    if round_move_count > 0:
        round_moves = state.moves[move_cursor:move_cursor + round_move_count]
        round_created = state.move_created_ids[
            move_cursor:move_cursor + round_move_count
        ]
        move_cursor += round_move_count
        await trace.record(
            await moves_to_trace_event(round_moves, round_created, db)
        )
    return move_cursor


def _prepare_tools(tools: list[Tool]) -> tuple[list[dict], dict]:
    """Build API tool definitions and function lookup from Tool list."""
    tool_defs = [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema}
        for t in tools
    ]
    tool_fns = {t.name: t.fn for t in tools}
    return tool_defs, tool_fns


async def run_single_call(
    system_prompt: str,
    user_message: str,
    tools: list[Tool],
    *,
    call_id: str,
    phase: str,
    db: DB,
    state: MoveState,
    trace: "CallTrace | None" = None,
    max_tokens: int = 4096,
) -> AgentResult:
    """Single LLM call with tools, plus exchange/trace persistence.

    Executes tool calls but does NOT loop back. Used for phase-1 page
    loading and single-call prioritization.
    """
    settings = get_settings()
    client = anthropic.AsyncAnthropic(api_key=settings.require_anthropic_key())
    tool_defs, tool_fns = _prepare_tools(tools)
    move_tool_names = {md.name for md in MOVES.values()}

    log.debug(
        "run_single_call: phase=%s, max_tokens=%d, tools=%s",
        phase, max_tokens, [t.name for t in tools],
    )

    messages: list[dict] = [{"role": "user", "content": user_message}]
    all_warnings: list[str] = []
    meta = LLMExchangeMetadata(
        call_id=call_id, phase=phase, trace=trace,
        user_message=user_message,
    )
    api_resp = await call_api(
        client, settings.model, system_prompt, messages,
        tool_defs or None, max_tokens, warnings=all_warnings,
        metadata=meta, db=db,
    )
    response = api_resp.message

    text_parts: list[str] = []
    tool_uses: list[ToolUseBlock] = []
    for block in response.content:
        if isinstance(block, TextBlock):
            text_parts.append(block.text)
        elif isinstance(block, ToolUseBlock):
            tool_uses.append(block)

    all_tool_calls, _ = await _execute_tool_uses(tool_uses, tool_fns)

    rr = RoundRecord(
        round=0,
        response_text="\n".join(text_parts),
        tool_calls=all_tool_calls,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        duration_ms=api_resp.duration_ms,
    )

    if trace:
        await _record_round_moves(
            rr,
            trace=trace,
            state=state,
            move_cursor=len(state.moves),
            move_tool_names=move_tool_names,
            db=db,
        )
    for w in all_warnings:
        if trace:
            await trace.record(WarningEvent(message=w))

    log.info(
        "run_single_call complete: %d tool calls, %d text chars",
        len(all_tool_calls), sum(len(t) for t in text_parts),
    )
    return AgentResult(
        text="\n".join(text_parts),
        tool_calls=all_tool_calls,
        rounds=[rr],
        system_prompt=system_prompt,
        user_message=user_message,
        warnings=all_warnings,
    )


async def run_agent_loop(
    system_prompt: str,
    user_message: str,
    tools: list[Tool],
    *,
    call_id: str,
    db: DB,
    state: MoveState,
    trace: "CallTrace | None" = None,
    max_tokens: int = 4096,
    max_rounds: int | None = None,
) -> AgentResult:
    """Tool-use conversation loop with per-round exchange/trace persistence.

    Each Tool's fn is called when the LLM invokes it. The fn's return value
    is sent back as the tool_result content. If fn raises, the exception
    message is sent back as an error result.
    """
    settings = get_settings()
    effective_rounds = max_rounds if max_rounds is not None else (
        2 if settings.is_smoke_test else 6
    )
    client = anthropic.AsyncAnthropic(api_key=settings.require_anthropic_key())
    tool_defs, tool_fns = _prepare_tools(tools)
    move_tool_names = {md.name for md in MOVES.values()}

    log.debug(
        "run_agent_loop starting: max_rounds=%d, max_tokens=%d, tools=%s",
        effective_rounds, max_tokens, [t.name for t in tools],
    )

    messages: list[dict] = [{"role": "user", "content": user_message}]
    text_parts: list[str] = []
    all_tool_calls: list[ToolCall] = []
    all_rounds: list[RoundRecord] = []
    all_warnings: list[str] = []
    move_cursor = len(state.moves)
    round_num = 0

    for round_num in range(effective_rounds + 1):
        log.debug("run_agent_loop round %d/%d", round_num + 1, effective_rounds)
        meta = LLMExchangeMetadata(
            call_id=call_id, phase="inner_loop", trace=trace,
            round_num=round_num,
            user_message=user_message if round_num == 0 else None,
        )
        api_resp = await call_api(
            client, settings.model, system_prompt, messages,
            tool_defs or None, max_tokens, warnings=all_warnings,
            metadata=meta, db=db,
        )
        response = api_resp.message

        tool_uses: list[ToolUseBlock] = []
        round_text_parts: list[str] = []
        for block in response.content:
            if isinstance(block, TextBlock):
                text_parts.append(block.text)
                round_text_parts.append(block.text)
            elif isinstance(block, ToolUseBlock):
                tool_uses.append(block)

        if response.stop_reason == "end_turn" or not tool_uses:
            log.debug(
                "run_agent_loop ending: stop_reason=%s, tool_uses=%d, rounds_used=%d",
                response.stop_reason, len(tool_uses), round_num + 1,
            )
            rr = RoundRecord(
                round=round_num,
                response_text="\n".join(round_text_parts),
                tool_calls=[],
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                duration_ms=api_resp.duration_ms,
            )
            all_rounds.append(rr)
            break

        log.debug(
            "run_agent_loop round %d: %d tool call(s): %s",
            round_num + 1, len(tool_uses), [tu.name for tu in tool_uses],
        )

        round_tool_calls, tool_results = await _execute_tool_uses(tool_uses, tool_fns)
        all_tool_calls.extend(round_tool_calls)

        rr = RoundRecord(
            round=round_num,
            response_text="\n".join(round_text_parts),
            tool_calls=round_tool_calls,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            duration_ms=api_resp.duration_ms,
        )
        all_rounds.append(rr)
        if trace:
            move_cursor = await _record_round_moves(
                rr,
                trace=trace,
                state=state,
                move_cursor=move_cursor,
                move_tool_names=move_tool_names,
                db=db,
            )

        remaining = effective_rounds - round_num
        if remaining == 1:
            budget_note = {
                "type": "text",
                "text": "This is your final round — finish your work now.",
            }
        else:
            budget_note = {
                "type": "text",
                "text": (
                    f"After this round of tool calls, you will have "
                    f"{remaining - 1} rounds remaining."
                ),
            }

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results + [budget_note]})

    for w in all_warnings:
        if trace:
            await trace.record(WarningEvent(message=w))

    log.info(
        "run_agent_loop complete: %d rounds, %d tool calls, %d text chars",
        round_num + 1, len(all_tool_calls), sum(len(t) for t in text_parts),
    )
    return AgentResult(
        text="\n".join(text_parts),
        tool_calls=all_tool_calls,
        rounds=all_rounds,
        system_prompt=system_prompt,
        user_message=user_message,
        warnings=all_warnings,
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
    call_id: str,
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
        result = await run_single_call(
            system_prompt=system_prompt,
            user_message=phase1_msg,
            tools=[load_page_tool],
            call_id=call_id,
            phase="initial_page_loads",
            db=db,
            state=state,
            trace=trace,
            max_tokens=2048,
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
        phase1_ids = await _run_phase1(
            system_prompt, context_text, call.id, state, db, trace=trace,
        )
        if phase1_ids:
            extra_text = await _format_loaded_pages(phase1_ids, db)
            context_text = context_text + "\n\n## Loaded Pages\n\n" + extra_text

    if call_type == CallType.PRIORITIZATION:
        from differential.moves.create_question import PRIORITIZATION_MOVE
        tools = []
        for mt in available_moves:
            if mt == MoveType.CREATE_QUESTION:
                tools.append(PRIORITIZATION_MOVE.bind(state))
            else:
                tools.append(MOVES[mt].bind(state))
    else:
        tools = [MOVES[mt].bind(state) for mt in available_moves]
    if call_type == CallType.PRIORITIZATION:
        for ddef in DISPATCH_DEFS.values():
            tools.append(ddef.bind(state, subtree_ids, short_id_map))

    user_message = build_user_message(context_text, task_description)

    if call_type == CallType.PRIORITIZATION:
        agent_result = await run_single_call(
            system_prompt, user_message, tools,
            call_id=call.id,
            phase="prioritization",
            db=db,
            state=state,
            trace=trace,
            max_tokens=max_tokens,
        )
    else:
        agent_result = await run_agent_loop(
            system_prompt, user_message, tools,
            call_id=call.id,
            db=db,
            state=state,
            trace=trace,
            max_tokens=max_tokens,
            max_rounds=max_rounds,
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


async def _resolve_payload_refs(move: Move, db: DB) -> list[PageRef]:
    """Resolve page IDs referenced in a move's payload fields."""
    fields = PAGE_ID_FIELDS.get(move.move_type, [])
    refs: list[PageRef] = []
    for field_name in fields:
        raw = getattr(move.payload, field_name, None)
        if not raw:
            continue
        full_id = await db.resolve_page_id(raw)
        if not full_id:
            continue
        page = await db.get_page(full_id)
        summary = page.summary if page else ""
        refs.append(PageRef(id=full_id, summary=summary))
    return refs


async def moves_to_trace_event(
    moves: list[Move],
    move_created_ids: list[list[str]],
    db: DB,
) -> MovesExecutedEvent:
    """Build a typed MovesExecutedEvent from a list of moves."""
    trace_items = []
    for i, m in enumerate(moves):
        created_ids = move_created_ids[i] if i < len(move_created_ids) else []
        created_refs = await resolve_page_refs(created_ids, db)
        payload_refs = await _resolve_payload_refs(m, db)
        seen = {r.id for r in created_refs}
        for pr in payload_refs:
            if pr.id not in seen:
                created_refs.append(pr)
                seen.add(pr.id)
        payload_data = m.payload.model_dump(exclude_none=True, exclude_defaults=True)
        trace_items.append(MoveTraceItem(
            type=m.move_type.value,
            page_refs=created_refs,
            **payload_data,
        ))
    return MovesExecutedEvent(moves=trace_items)


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
    trace: CallTrace | None = None,
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
        meta = LLMExchangeMetadata(
            call_id=call.id, phase="closing_review",
            trace=trace, user_message=user_message,
        ) if db else None
        result = await structured_call(
            system_prompt=REVIEW_SYSTEM_PROMPT,
            user_message=user_message,
            response_model=ReviewResponse,
            max_tokens=2048,
            metadata=meta,
            db=db,
        )
        review = result.data
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
