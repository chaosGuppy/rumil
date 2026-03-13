"""Shared utilities for call types."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from typing import Annotated, Literal

import anthropic
from anthropic.types import TextBlock, ToolUseBlock
from pydantic import BaseModel, Discriminator, Field, Tag

from rumil.context import assemble_call_context, format_page
from rumil.database import DB
from rumil.settings import get_settings
from rumil.workspace_map import build_workspace_map
from rumil.llm import (
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
from rumil.models import (
    Call,
    CallStatus,
    CallType,
    Dispatch,
    LinkType,
    Move,
    MoveType,
    PageLink,
)
from rumil.moves.base import MoveState
from rumil.moves.load_page import LoadPagePayload
from rumil.moves.registry import MOVES
from rumil.tracing.trace_events import (
    MoveTraceItem,
    MovesExecutedEvent,
    PageRef,
    WarningEvent,
)
from rumil.tracing.tracer import CallTrace

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
    *,
    trace: CallTrace,
    state: MoveState,
    db: DB,
) -> None:
    """Record a trace event for any moves added since the last call."""
    round_moves, round_created, round_extras = state.take_new_moves()
    if round_moves:
        await trace.record(
            await moves_to_trace_event(round_moves, round_created, db, round_extras)
        )


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
    user_message: str = "",
    tools: list[Tool] | None = None,
    *,
    call_id: str,
    phase: str,
    db: DB,
    state: MoveState,
    trace: "CallTrace | None" = None,
    max_tokens: int = 4096,
    messages: list[dict] | None = None,
    cache: bool = False,
) -> AgentResult:
    """Single LLM call with tools, plus exchange/trace persistence.

    Executes tool calls but does NOT loop back. Used for phase-1 page
    loading, single-call prioritization, and review link modification.

    Pass `messages` to resume a prior conversation, or `user_message` for
    a fresh single-turn call.
    """
    if not user_message and not messages:
        raise ValueError("Either user_message or messages must be provided")
    settings = get_settings()
    client = anthropic.AsyncAnthropic(api_key=settings.require_anthropic_key())
    if tools is not None:
        tool_defs, tool_fns = _prepare_tools(tools)
    else:
        tool_defs = []
        tool_fns = {}

    log.debug(
        "run_single_call: phase=%s, max_tokens=%d, tools=%d, resuming=%s",
        phase, max_tokens, len(tool_defs), messages is not None,
    )

    msg_list: list[dict] = (
        messages if messages is not None
        else [{"role": "user", "content": user_message}]
    )
    all_warnings: list[str] = []
    meta = LLMExchangeMetadata(
        call_id=call_id, phase=phase, trace=trace,
        user_message=user_message if user_message else None,
    )
    api_resp = await call_api(
        client, settings.model, system_prompt, msg_list,
        tool_defs or None, max_tokens, warnings=all_warnings,
        metadata=meta, db=db, cache=cache,
    )
    response = api_resp.message

    text_parts: list[str] = []
    tool_uses: list[ToolUseBlock] = []
    for block in response.content:
        if isinstance(block, TextBlock):
            text_parts.append(block.text)
        elif isinstance(block, ToolUseBlock):
            tool_uses.append(block)

    all_tool_calls, tool_results = await _execute_tool_uses(tool_uses, tool_fns)

    rr = RoundRecord(
        round=0,
        response_text="\n".join(text_parts),
        tool_calls=all_tool_calls,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        duration_ms=api_resp.duration_ms,
    )

    if trace:
        await _record_round_moves(trace=trace, state=state, db=db)
    for w in all_warnings:
        if trace:
            await trace.record(WarningEvent(message=w))

    msg_list.append({"role": "assistant", "content": response.content})
    if tool_results:
        msg_list.append({"role": "user", "content": tool_results})

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
        messages=msg_list,
    )


async def run_agent_loop(
    system_prompt: str,
    user_message: str = "",
    tools: list[Tool] | None = None,
    *,
    call_id: str,
    db: DB,
    state: MoveState,
    trace: "CallTrace | None" = None,
    max_tokens: int = 4096,
    max_rounds: int | None = None,
    messages: list[dict] | None = None,
    cache: bool = False,
) -> AgentResult:
    """Tool-use conversation loop with per-round exchange/trace persistence.

    Each Tool's fn is called when the LLM invokes it. The fn's return value
    is sent back as the tool_result content. If fn raises, the exception
    message is sent back as an error result.

    Pass `messages` to resume a prior conversation.
    """
    if not user_message and not messages:
        raise ValueError("Either user_message or messages must be provided")
    settings = get_settings()
    effective_rounds = max_rounds if max_rounds is not None else (
        2 if settings.is_smoke_test else 6
    )
    client = anthropic.AsyncAnthropic(api_key=settings.require_anthropic_key())
    if tools is not None:
        tool_defs, tool_fns = _prepare_tools(tools)
    else:
        tool_defs = []
        tool_fns = {}
    log.debug(
        "run_agent_loop starting: max_rounds=%d, max_tokens=%d, resuming=%s",
        effective_rounds, max_tokens, messages is not None,
    )

    msg_list: list[dict] = (
        messages if messages is not None
        else [{"role": "user", "content": user_message}]
    )
    text_parts: list[str] = []
    all_tool_calls: list[ToolCall] = []
    all_rounds: list[RoundRecord] = []
    all_warnings: list[str] = []
    round_num = 0

    for round_num in range(effective_rounds + 1):
        log.debug("run_agent_loop round %d/%d", round_num + 1, effective_rounds)
        meta = LLMExchangeMetadata(
            call_id=call_id, phase="inner_loop", trace=trace,
            round_num=round_num,
            user_message=user_message if round_num == 0 else None,
        )
        api_resp = await call_api(
            client, settings.model, system_prompt, msg_list,
            tool_defs or None, max_tokens, warnings=all_warnings,
            metadata=meta, db=db, cache=cache,
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
            msg_list.append({"role": "assistant", "content": response.content})
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
            await _record_round_moves(trace=trace, state=state, db=db)

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

        msg_list.append({"role": "assistant", "content": response.content})
        msg_list.append({"role": "user", "content": tool_results + [budget_note]})

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
        messages=msg_list,
    )


class PageRating(BaseModel):
    page_id: str = Field(description="Short ID of the rated page")
    score: int = Field(
        description="-1 = confusing, 0 = no help, 1 = helpful, 2 = very helpful"
    )
    note: str = Field("", description="One sentence on why")


class ReviewLinkConsideration(BaseModel):
    link_type: Literal["consideration"] = "consideration"
    page_id: str = Field(description="Short ID of the page to link to the scope question")
    strength: float = Field(
        2.5,
        description="0-5: how strongly this bears on the question",
    )
    reasoning: str = Field("", description="Why this page bears on the question")


class ReviewLinkChildQuestion(BaseModel):
    link_type: Literal["child_question"] = "child_question"
    page_id: str = Field(description="Short ID of the child question page")
    reasoning: str = Field("", description="Why this is a sub-question")


class ReviewLinkRelated(BaseModel):
    link_type: Literal["related"] = "related"
    page_id: str = Field(description="Short ID of the page to link")
    reasoning: str = Field("", description="Nature of the relation")


ReviewLink = Annotated[
    Annotated[ReviewLinkConsideration, Tag("consideration")]
    | Annotated[ReviewLinkChildQuestion, Tag("child_question")]
    | Annotated[ReviewLinkRelated, Tag("related")],
    Discriminator("link_type"),
]


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
    links: list[ReviewLink] = Field(
        default_factory=list,
        description=(
            "Links to create between loaded pages and the scope question. "
            "Include a link for each page rated helpful (1) or very helpful (2)."
        ),
    )


@dataclass
class RunCallResult:
    """Result of a run_call invocation."""

    created_page_ids: list[str] = field(default_factory=list)
    dispatches: list[Dispatch] = field(default_factory=list)
    moves: list[Move] = field(default_factory=list)
    phase1_page_ids: list[str] = field(default_factory=list)
    loaded_page_summaries: list[tuple[str, str]] = field(default_factory=list)
    agent_result: AgentResult = field(default_factory=AgentResult)


async def _format_loaded_pages(
    page_ids: list[str], db: DB,
) -> tuple[str, list[tuple[str, str]]]:
    """Format loaded pages as context text for phase 2.

    Returns (formatted_text, page_summaries) where page_summaries is a list
    of (full_page_id, summary) tuples for use in the closing review.
    """
    parts: list[str] = []
    summaries: list[tuple[str, str]] = []
    for pid in page_ids:
        page = await db.get_page(pid)
        if page:
            parts.append(f"### Page `{pid[:8]}`\n\n{await format_page(page, db=db)}")
            summaries.append((pid, page.summary))
    return "\n\n---\n\n".join(parts), summaries


async def _run_initial_page_loading(
    system_prompt: str,
    working_context: str,
    workspace_map: str,
    call_id: str,
    state: MoveState,
    db: DB,
    trace: CallTrace | None = None,
) -> list[str]:
    """Preliminary page loading via single LLM call with load_page tool.

    Assembles its own context from working_context and workspace_map.
    Returns resolved full page IDs. Free (not counted against budget).
    """
    context_text = assemble_call_context(working_context, workspace_map=workspace_map)
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
    working_context: str,
    call: Call,
    db: DB,
    *,
    available_moves: list[MoveType] | None = None,
    max_tokens: int = 4096,
    max_rounds: int | None = None,
    trace: "CallTrace | None" = None,
) -> RunCallResult:
    """Run a workspace call (scout/assess/ingest) with tool use.

    Builds workspace maps internally and assembles context for each phase:
      - Phase 1 (initial page loading): filtered map showing only pages
        added since the last successful call of the same type. Skipped
        entirely if there are no new pages.
      - Phase 2 (main call): full workspace map + loaded pages

    Returns a RunCallResult with created page IDs, dispatches, loaded page
    summaries, and the raw agent result.
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
    loaded_page_summaries: list[tuple[str, str]] = []
    extra_pages_text: str | None = None

    last_call_time = await db.get_last_successful_call_time(
        call_type, call.scope_page_id or "",
    ) if call.scope_page_id else None

    if last_call_time:
        filtered_map, filtered_ids = await build_workspace_map(
            db, created_after=last_call_time,
        )
        if not filtered_ids:
            log.info("Skipping initial page loading: no new pages since last call")
        else:
            phase1_ids = await _run_initial_page_loading(
                system_prompt, working_context, filtered_map,
                call.id, state, db, trace=trace,
            )
    else:
        full_map_for_phase1, _ = await build_workspace_map(db)
        phase1_ids = await _run_initial_page_loading(
            system_prompt, working_context, full_map_for_phase1,
            call.id, state, db, trace=trace,
        )

    if phase1_ids:
        extra_pages_text, loaded_page_summaries = await _format_loaded_pages(
            phase1_ids, db,
        )

    workspace_map, _ = await build_workspace_map(db)
    phase2_context = assemble_call_context(
        working_context, workspace_map=workspace_map,
        extra_pages_text=extra_pages_text,
    )

    tools = [MOVES[mt].bind(state) for mt in available_moves]
    user_message = build_user_message(phase2_context, task_description)

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
        loaded_page_summaries=loaded_page_summaries,
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
    trace_extras: list[dict] | None = None,
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
        extra = trace_extras[i] if trace_extras and i < len(trace_extras) else {}
        trace_items.append(MoveTraceItem(
            type=m.move_type.value,
            page_refs=created_refs,
            **payload_data,
            **extra,
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


async def complete_call(call: Call, db: DB, summary: str) -> None:
    call.status = CallStatus.COMPLETE
    call.completed_at = datetime.now(UTC)
    call.result_summary = summary
    await db.save_call(call)


@dataclass
class ClosingReviewResult:
    """Result of a closing review. Check `error` to determine success."""

    data: dict = field(default_factory=dict)
    error: str | None = None


async def _execute_review_links(
    links: list[dict],
    scope_question_id: str,
    db: DB,
) -> None:
    """Execute links returned by the closing review.

    All links are between a loaded page and the scope question.
    """
    for link_data in links:
        link_type = link_data.get("link_type")
        page_id = await db.resolve_page_id(link_data.get("page_id", ""))
        if not page_id:
            log.warning(
                "Review link skipped: unresolved page_id=%s",
                link_data.get("page_id"),
            )
            continue
        try:
            if link_type == "consideration":
                link = PageLink(
                    from_page_id=page_id,
                    to_page_id=scope_question_id,
                    link_type=LinkType.CONSIDERATION,
                    strength=link_data.get("strength", 2.5),
                    reasoning=link_data.get("reasoning", ""),
                )
                await db.save_link(link)
                log.info(
                    "Review link: consideration %s -> %s (%.1f)",
                    page_id[:8], scope_question_id[:8], link.strength,
                )
            elif link_type == "child_question":
                link = PageLink(
                    from_page_id=scope_question_id,
                    to_page_id=page_id,
                    link_type=LinkType.CHILD_QUESTION,
                    reasoning=link_data.get("reasoning", ""),
                )
                await db.save_link(link)
                log.info(
                    "Review link: child_question %s -> %s",
                    scope_question_id[:8], page_id[:8],
                )
            elif link_type == "related":
                link = PageLink(
                    from_page_id=page_id,
                    to_page_id=scope_question_id,
                    link_type=LinkType.RELATED,
                    reasoning=link_data.get("reasoning", ""),
                )
                await db.save_link(link)
                log.info(
                    "Review link: related %s -> %s",
                    page_id[:8], scope_question_id[:8],
                )
            else:
                log.warning("Review link: unknown type '%s'", link_type)
        except Exception as e:
            log.warning("Review link failed: %s", e, exc_info=True)


async def run_closing_review(
    call: Call,
    main_output: str,
    working_context: str,
    loaded_page_summaries: list[tuple[str, str]] | None = None,
    db: DB | None = None,
    trace: CallTrace | None = None,
    scope_question_id: str | None = None,
) -> ClosingReviewResult:
    """Run the closing review as a separate call. Free (not counted against budget).

    Uses working_context with no workspace map for the review context.
    loaded_page_summaries is a list of (page_id, summary) tuples — no DB
    fetch needed for page info. When scope_question_id is provided, the
    review prompt instructs the LLM to link helpful pages to the scope question.
    """
    page_rating_note = ""
    if loaded_page_summaries:
        page_lines = [
            f'  - `{pid[:8]}`: "{summary[:120]}"'
            for pid, summary in loaded_page_summaries
        ]
        if page_lines:
            scope_note = ""
            if scope_question_id:
                scope_note = (
                    '\n\nFor each page you rate as helpful (1) or very helpful (2), '
                    'include a link in the `links` field to connect it to the scope '
                    f'question `{scope_question_id[:8]}`. Use link_type "consideration" '
                    'for claims (with strength), "child_question" for '
                    'sub-questions, or "related" for other page types.'
                )
            page_rating_note = (
                '\n\nThe following pages were loaded into your context beyond the base '
                'working context:\n'
                + '\n'.join(page_lines)
                + '\n\nPlease include a rating for each in your page_ratings. '
                'Scores: -1 = actively confusing, 0 = didn\'t help, '
                '1 = helped, 2 = extremely helpful.'
                + scope_note
            )

    review_task = (
        f"You have just completed a {call.call_type.value} call.\n\n"
        f"Here is your output from that call:\n{main_output}\n\n"
        "Please review your work and provide your assessment."
        f"{page_rating_note}"
    )

    context_text = assemble_call_context(working_context)
    log.debug(
        "Closing review starting: call=%s, type=%s, loaded_pages=%d",
        call.id[:8], call.call_type.value, len(loaded_page_summaries or []),
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
            max_tokens=4096,
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
                review_links = review.get("links", [])
                if review_links and scope_question_id:
                    await _execute_review_links(review_links, scope_question_id, db)
            return ClosingReviewResult(data=review)
        else:
            log.warning("Closing review returned empty data for call=%s", call.id[:8])
            return ClosingReviewResult(error="structured_call returned empty data")
    except Exception as e:
        log.error(
            "Closing review failed for call=%s: %s", call.id[:8], e, exc_info=True,
        )
        return ClosingReviewResult(error=str(e))


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

