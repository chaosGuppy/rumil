"""Shared utilities for call types."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

import anthropic
from anthropic.types import TextBlock, ToolUseBlock
from pydantic import BaseModel, Field

from rumil.database import DB
from rumil.embeddings import embed_and_store_page
from rumil.llm import (
    AgentResult,
    LLMExchangeMetadata,
    RoundRecord,
    Tool,
    ToolCall,
    build_user_message,
    call_api,
    structured_call,
)
from rumil.models import (
    Call,
    CallStatus,
    Dispatch,
    Move,
    MoveType,
    Page,
)
from rumil.moves.base import MoveState
from rumil.settings import get_settings
from rumil.tracing.trace_events import (
    ErrorEvent,
    MovesExecutedEvent,
    MoveTraceItem,
    PageRef,
    WarningEvent,
)
from rumil.tracing.tracer import get_trace

log = logging.getLogger(__name__)

PAGE_ID_FIELDS: dict[MoveType, list[str]] = {
    MoveType.LOAD_PAGE: ["page_id"],
    MoveType.LINK_CONSIDERATION: ["claim_id", "question_id"],
    MoveType.LINK_CHILD_QUESTION: ["child_id", "parent_id"],
    MoveType.LINK_RELATED: ["from_page_id", "to_page_id"],
    MoveType.LINK_VARIANT: ["variant_page_id", "original_page_id"],
    MoveType.FLAG_FUNNINESS: ["page_id"],
    MoveType.REPORT_DUPLICATE: ["page_id_a", "page_id_b"],
}


async def execute_tool_uses(
    tool_uses: Sequence[ToolUseBlock],
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
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": result_str,
                    "is_error": True,
                }
            )
        else:
            try:
                result_str = await fn(tu.input)
            except Exception as e:
                log.error(
                    "Tool %s raised an exception: %s",
                    tu.name,
                    e,
                    exc_info=True,
                )
                result_str = f"Error: {e}"
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": result_str,
                        "is_error": True,
                    }
                )
                trace = get_trace()
                if trace:
                    await trace.record(
                        ErrorEvent(
                            message=f"Tool {tu.name} error: {e}",
                            phase="tool_execution",
                        )
                    )
            else:
                log.debug(
                    "Tool %s returned: %s",
                    tu.name,
                    result_str[:200] if result_str else "(empty)",
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": result_str,
                    }
                )
        tool_calls.append(ToolCall(name=tu.name, input=tu.input, result=result_str))
    return tool_calls, tool_results


async def record_round_moves(
    *,
    state: MoveState,
    db: DB,
) -> None:
    """Record a trace event for any moves added since the last call.

    Uses ``record_strict`` because moves have already been applied to the DB
    at this point — a silently-dropped trace event leaves the frontend view
    of the call out of sync with actual workspace state.
    """
    trace = get_trace()
    round_moves, round_created, round_extras = state.take_new_moves()
    if round_moves and trace:
        await trace.record_strict(
            await moves_to_trace_event(round_moves, round_created, db, round_extras)
        )


def prepare_tools(tools: Sequence[Tool]) -> tuple[list[dict], dict]:
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
    messages: list[dict] | None = None,
    cache: bool = False,
) -> AgentResult:
    """Single LLM call with tools, plus exchange/trace persistence.

    Executes tool calls but does NOT loop back. Used for single-call
    prioritization and review link modification.

    Pass `messages` to resume a prior conversation, or `user_message` for
    a fresh single-turn call.
    """
    if not user_message and not messages:
        raise ValueError("Either user_message or messages must be provided")
    settings = get_settings()
    client = anthropic.AsyncAnthropic(api_key=settings.require_anthropic_key())
    if tools is not None:
        tool_defs, tool_fns = prepare_tools(tools)
    else:
        tool_defs = []
        tool_fns = {}

    log.debug(
        "run_single_call: phase=%s, tools=%d, resuming=%s",
        phase,
        len(tool_defs),
        messages is not None,
    )

    msg_list: list[dict] = (
        messages if messages is not None else [{"role": "user", "content": user_message}]
    )
    all_warnings: list[str] = []
    meta = LLMExchangeMetadata(
        call_id=call_id,
        phase=phase,
        user_message=user_message if user_message else None,
    )
    api_resp = await call_api(
        client,
        settings.model,
        system_prompt,
        msg_list,
        tool_defs or None,
        warnings=all_warnings,
        metadata=meta,
        db=db,
        cache=cache,
    )
    response = api_resp.message

    text_parts: list[str] = []
    tool_uses: list[ToolUseBlock] = []
    for block in response.content:
        if isinstance(block, TextBlock):
            text_parts.append(block.text)
        elif isinstance(block, ToolUseBlock):
            tool_uses.append(block)

    all_tool_calls, tool_results = await execute_tool_uses(tool_uses, tool_fns)

    rr = RoundRecord(
        round=0,
        response_text="\n".join(text_parts),
        tool_calls=all_tool_calls,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        duration_ms=api_resp.duration_ms,
    )

    await record_round_moves(state=state, db=db)
    trace = get_trace()
    for w in all_warnings:
        if trace:
            await trace.record(WarningEvent(message=w))

    msg_list.append({"role": "assistant", "content": response.content})
    if tool_results:
        msg_list.append({"role": "user", "content": tool_results})

    log.info(
        "run_single_call complete: %d tool calls, %d text chars",
        len(all_tool_calls),
        sum(len(t) for t in text_parts),
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
    effective_rounds = (
        max_rounds if max_rounds is not None else (2 if settings.is_smoke_test else 3)
    )
    client = anthropic.AsyncAnthropic(api_key=settings.require_anthropic_key())
    if tools is not None:
        tool_defs, tool_fns = prepare_tools(tools)
    else:
        tool_defs = []
        tool_fns = {}
    log.debug(
        "run_agent_loop starting: max_rounds=%d, resuming=%s",
        effective_rounds,
        messages is not None,
    )

    msg_list: list[dict] = (
        messages if messages is not None else [{"role": "user", "content": user_message}]
    )
    text_parts: list[str] = []
    all_tool_calls: list[ToolCall] = []
    all_rounds: list[RoundRecord] = []
    all_warnings: list[str] = []
    round_num = 0

    for round_num in range(effective_rounds):
        log.debug("run_agent_loop round %d/%d", round_num + 1, effective_rounds)
        meta = LLMExchangeMetadata(
            call_id=call_id,
            phase="inner_loop",
            round_num=round_num,
            user_message=user_message if round_num == 0 else None,
        )
        api_resp = await call_api(
            client,
            settings.model,
            system_prompt,
            msg_list,
            tool_defs or None,
            warnings=all_warnings,
            metadata=meta,
            db=db,
            cache=cache,
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
                response.stop_reason,
                len(tool_uses),
                round_num + 1,
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
            round_num + 1,
            len(tool_uses),
            [tu.name for tu in tool_uses],
        )

        round_tool_calls, tool_results = await execute_tool_uses(tool_uses, tool_fns)
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
        await record_round_moves(state=state, db=db)

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
                    "After this round of tool calls, you will have "
                    f"{remaining - 1} rounds remaining."
                ),
            }

        msg_list.append({"role": "assistant", "content": response.content})
        msg_list.append({"role": "user", "content": [*tool_results, budget_note]})

    trace = get_trace()
    for w in all_warnings:
        if trace:
            await trace.record(WarningEvent(message=w))

    log.info(
        "run_agent_loop complete: %d rounds, %d tool calls, %d text chars",
        round_num + 1,
        len(all_tool_calls),
        sum(len(t) for t in text_parts),
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


ABSTRACT_INSTRUCTION = (
    "Self-contained summary of ~200 words that makes sense with zero prior context. "
    "Preserve epistemic qualifications, confidence levels, and priority orderings.\n"
    "For CLAIM pages: the abstract is the pure assertion — fully detailed statement "
    "of what the claim asserts (specifics, numbers, scope), but WITHOUT derivation, "
    "provenance, or dependency information. No 'this rests on X', no 'because of Y', "
    "no citations. The abstract says WHAT the claim asserts; the content says WHY.\n"
    "For all other page types (judgement, question, source, etc.): include the core "
    "conclusion, main supporting reasoning or evidence, key counter-arguments and why "
    "they were discounted, and the critical uncertainties or dependencies."
)


class PageSummaryItem(BaseModel):
    page_id: str = Field(description="Full or short ID of the page")
    abstract: str = Field(description=ABSTRACT_INSTRUCTION)


async def save_page_abstracts(
    summaries: Sequence[PageSummaryItem],
    db: DB,
) -> None:
    """Persist abstracts and re-embed pages. Shared by closing reviewers
    and the grounding feedback pipeline."""

    sem = asyncio.Semaphore(10)

    async def _save_one(s: PageSummaryItem) -> None:
        async with sem:
            pid = await db.resolve_page_id(s.page_id)
            if not pid:
                return
            await db.update_page_abstract(pid, s.abstract)
            if not s.abstract.strip():
                return
            page = await db.get_page(pid)
            if not page:
                return
            try:
                await embed_and_store_page(db, page, field_name="abstract")
            except Exception:
                log.warning("Failed to re-embed page %s", pid[:8], exc_info=True)

    await asyncio.gather(*(_save_one(s) for s in summaries))


class PageRating(BaseModel):
    page_id: str = Field(description="Short ID of the rated page")
    score: int = Field(description="-1 = confusing, 0 = no help, 1 = helpful, 2 = very helpful")
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
    confidence_in_output: float = Field(description="0-5 confidence in the work just done")
    context_was_adequate: bool = Field(description="Whether the context provided was sufficient")
    what_was_missing: str = Field("", description="What additional context would have helped")
    tensions_noticed: str = Field("", description="Any conflicts or inconsistencies noticed")
    self_assessment: str = Field("", description="1-2 sentences on how this call went")
    suggested_next_steps: str = Field("", description="What should happen next")
    page_ratings: list[PageRating] = Field(
        default_factory=list,
        description="Ratings for pages that were loaded into context",
    )
    page_summaries: list[PageSummaryItem] = Field(
        default_factory=list,
        description=(
            "Abstract for each page you created during this call. "
            "Provide one entry per created page."
        ),
    )


@dataclass
class RunCallResult:
    """Result of an LLM-driven call invocation."""

    created_page_ids: list[str] = field(default_factory=list)
    dispatches: list[Dispatch] = field(default_factory=list)
    moves: list[Move] = field(default_factory=list)
    agent_result: AgentResult = field(default_factory=AgentResult)


async def resolve_page_refs(page_ids: Sequence[str], db: DB) -> list[PageRef]:
    """Resolve a list of page IDs to PageRef objects with headlines."""
    pages = await db.get_pages_by_ids(page_ids)
    return [
        PageRef(id=pid, headline=pages[pid].headline if pid in pages else "") for pid in page_ids
    ]


def _payload_raw_refs(move: Move) -> list[str]:
    """Extract raw page ID strings from a move's payload fields."""
    fields = PAGE_ID_FIELDS.get(move.move_type, [])
    raws: list[str] = []
    for field_name in fields:
        raw = getattr(move.payload, field_name, None)
        if raw:
            raws.append(raw)
    return raws


async def moves_to_trace_event(
    moves: Sequence[Move],
    move_created_ids: Sequence[Sequence[str]],
    db: DB,
    trace_extras: Sequence[dict] | None = None,
) -> MovesExecutedEvent:
    """Build a typed MovesExecutedEvent from a list of moves."""
    raw_payload_refs = [_payload_raw_refs(m) for m in moves]
    all_raw_refs = [raw for refs in raw_payload_refs for raw in refs]
    resolved_payload_ids = await db.resolve_page_ids(all_raw_refs)

    all_full_ids: set[str] = set()
    for created in move_created_ids:
        all_full_ids.update(created)
    all_full_ids.update(resolved_payload_ids.values())
    pages = await db.get_pages_by_ids(list(all_full_ids))

    def _ref(full_id: str) -> PageRef:
        return PageRef(
            id=full_id,
            headline=pages[full_id].headline if full_id in pages else "",
        )

    trace_items = []
    for i, m in enumerate(moves):
        created_ids = move_created_ids[i] if i < len(move_created_ids) else []
        created_refs = [_ref(pid) for pid in created_ids]
        seen = {r.id for r in created_refs}
        for raw in raw_payload_refs[i]:
            full_id = resolved_payload_ids.get(raw)
            if not full_id or full_id in seen:
                continue
            created_refs.append(_ref(full_id))
            seen.add(full_id)
        payload_data = m.payload.model_dump(exclude_none=True, exclude_defaults=True)
        extra = trace_extras[i] if trace_extras and i < len(trace_extras) else {}
        trace_items.append(
            MoveTraceItem(
                type=m.move_type.value,
                page_refs=created_refs,
                **payload_data,
                **extra,
            )
        )
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


async def mark_call_completed(call: Call, db: DB, summary: str) -> None:
    call.status = CallStatus.COMPLETE
    call.completed_at = datetime.now(UTC)
    call.result_summary = summary
    trace = get_trace()
    if trace and trace.total_cost_usd > 0:
        call.cost_usd = trace.total_cost_usd
    await db.save_call(call)


async def run_closing_review(
    call: Call,
    main_output: str,
    context_text: str,
    loaded_page_ids: list[str] | None = None,
    created_page_ids: list[str] | None = None,
    db: DB | None = None,
) -> dict | None:
    """Run the closing review as a separate call. Free (not counted against budget)."""
    review_pages: dict[str, Page] = {}
    if db and (loaded_page_ids or created_page_ids):
        review_pages = await db.get_pages_by_ids(
            list(loaded_page_ids or []) + list(created_page_ids or [])
        )

    page_rating_note = ""
    if loaded_page_ids and db:
        page_lines = []
        for pid in loaded_page_ids:
            page = review_pages.get(pid)
            if page:
                page_lines.append(f'  - `{pid[:8]}`: "{page.headline[:120]}"')
        if page_lines:
            page_rating_note = (
                "\n\nThe following pages were loaded into your context beyond the base "
                "working context:\n"
                + "\n".join(page_lines)
                + "\n\nPlease include a rating for each in your page_ratings. "
                "Scores: -1 = actively confusing, 0 = didn't help, "
                "1 = helped, 2 = extremely helpful."
            )

    page_summary_note = ""
    if created_page_ids and db:
        created_lines = []
        for pid in created_page_ids:
            page = review_pages.get(pid)
            if page:
                created_lines.append(f'  - `{pid[:8]}`: "{page.headline[:120]}"')
        if created_lines:
            page_summary_note = (
                "\n\nYou created the following pages during this call:\n"
                + "\n".join(created_lines)
                + "\n\nFor each, provide an abstract (~200 words, fully self-contained) "
                "in your page_summaries. "
                "These will be read by other LLM instances with no prior context, so do not "
                "assume any background knowledge."
            )

    review_task = (
        f"You have just completed a {call.call_type.value} call.\n\n"
        f"Here is your output from that call:\n{main_output}\n\n"
        "Please review your work and provide your assessment."
        f"{page_rating_note}"
        f"{page_summary_note}"
    )

    log.debug(
        "Closing review starting: call=%s, type=%s, loaded_pages=%d, created_pages=%d",
        call.id[:8],
        call.call_type.value,
        len(loaded_page_ids or []),
        len(created_page_ids or []),
    )
    try:
        user_message = build_user_message(context_text, review_task)
        meta = (
            LLMExchangeMetadata(
                call_id=call.id,
                phase="closing_review",
                user_message=user_message,
            )
            if db
            else None
        )
        result = await structured_call(
            system_prompt=REVIEW_SYSTEM_PROMPT,
            user_message=user_message,
            response_model=ReviewResponse,
            metadata=meta,
            db=db,
        )
        review = result.parsed.model_dump() if result.parsed else None
        if review:
            log.info(
                "Closing review complete: call=%s, fruit=%s, confidence=%s",
                call.id[:8],
                review.get("remaining_fruit"),
                review.get("confidence_in_output"),
            )
            if db:
                ratings = review.get("page_ratings", [])
                raw_rating_ids = [r.get("page_id", "") for r in ratings]
                resolved_rating_ids = await db.resolve_page_ids(
                    [rid for rid in raw_rating_ids if rid]
                )
                for r in ratings:
                    pid = resolved_rating_ids.get(r.get("page_id", ""))
                    score = r.get("score")
                    if pid and isinstance(score, int):
                        await db.save_page_rating(pid, call.id, score, r.get("note", ""))
                raw_summaries = review.get("page_summaries", [])
                items = [
                    PageSummaryItem(**s)
                    for s in raw_summaries
                    if isinstance(s, dict) and s.get("page_id")
                ]
                await save_page_abstracts(items, db)
        else:
            log.warning("Closing review returned None for call=%s", call.id[:8])
        return review
    except Exception as e:
        log.error(
            "Closing review failed for call=%s: %s",
            call.id[:8],
            e,
            exc_info=True,
        )
        trace = get_trace()
        if trace:
            await trace.record(
                ErrorEvent(
                    message=f"Closing review failed: {e}",
                    phase="closing_review",
                )
            )
        return None


def format_moves_for_review(moves: Sequence[Move]) -> str:
    """Format moves as readable text for closing review context."""
    if not moves:
        return (
            "This call recorded no structural moves. If the call type does "
            "its work outside of moves (e.g. update_view's per-phase edits), "
            "that work is not visible here — do not interpret this as a "
            "failed or empty call on its own."
        )
    parts = []
    for m in moves:
        headline = getattr(m.payload, "headline", "")
        if headline:
            parts.append(f"- {m.move_type.value}: {headline}")
        else:
            parts.append(f"- {m.move_type.value}")
    return "\n".join(parts)
