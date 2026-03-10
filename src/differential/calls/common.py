"""Shared utilities for call types."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from differential.context import format_page
from differential.database import DB
from differential.llm import (
    AgentResult,
    Tool,
    build_system_prompt,
    build_user_message,
    structured_call,
    agent_loop,
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

if TYPE_CHECKING:
    from differential.tracer import CallTrace


PHASE1_TASK = (
    "Review the workspace map above and decide if you need the full content of any "
    "pages before starting your main task. Use load_page for any pages you want to "
    "read. Write brief planning notes about your approach."
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


def _format_loaded_pages(page_ids: list[str], db: DB) -> str:
    """Format loaded pages as context text for phase 2."""
    parts = []
    for pid in page_ids:
        page = db.get_page(pid)
        if page:
            parts.append(f"### Page `{pid[:8]}`\n\n{format_page(page, db=db)}")
    return "\n\n---\n\n".join(parts)


def _run_phase1(
    system_prompt: str,
    context_text: str,
    state: MoveState,
    db: DB,
) -> list[str]:
    """Preliminary page loading. Returns resolved full page IDs. Free."""
    try:
        load_tool = MOVES[MoveType.LOAD_PAGE].bind(state)
        phase1_msg = build_user_message(context_text, PHASE1_TASK)
        agent_loop(
            system_prompt,
            phase1_msg,
            [load_tool],
            max_tokens=2048,
            max_rounds=3,
        )
        loaded_ids = []
        for m in state.moves:
            if m.move_type == MoveType.LOAD_PAGE:
                assert isinstance(m.payload, LoadPagePayload)
                full_id = db.resolve_page_id(m.payload.page_id)
                if full_id:
                    loaded_ids.append(full_id)
        if loaded_ids:
            labels = [db.page_label(pid) for pid in loaded_ids]
            print(f"  [phase1] Loaded pages: {', '.join(labels)}")
        return loaded_ids
    except Exception as e:
        status = getattr(e, "status_code", None)
        reason = f"HTTP {status}" if status else type(e).__name__
        print(f"  [phase1] Preliminary loading skipped ({reason}) — continuing.")
        return []


def run_call(
    call_type: CallType,
    task_description: str,
    context_text: str,
    call: Call,
    db: DB,
    *,
    available_moves: list[MoveType] | None = None,
    max_tokens: int = 4096,
    max_rounds: int = 3,
    subtree_ids: set[str] | None = None,
    short_id_map: dict[str, str] | None = None,
) -> RunCallResult:
    """Run a workspace call with tool use.

    For non-prioritization calls, runs a preliminary phase where the LLM can
    load pages before starting its main work. Moves are executed immediately
    when the LLM calls them. Returns a RunCallResult with created page IDs,
    dispatches, and the raw agent result.
    """

    if available_moves is None:
        available_moves = list(MoveType)

    state = MoveState(call, db)
    system_prompt = build_system_prompt(call_type.value)

    phase1_ids: list[str] = []
    if call_type != CallType.PRIORITIZATION:
        phase1_ids = _run_phase1(system_prompt, context_text, state, db)
        if phase1_ids:
            extra_text = _format_loaded_pages(phase1_ids, db)
            context_text = context_text + "\n\n## Loaded Pages\n\n" + extra_text

    tools = [MOVES[mt].bind(state) for mt in available_moves]
    if call_type == CallType.PRIORITIZATION:
        for ddef in DISPATCH_DEFS.values():
            tools.append(ddef.bind(state, subtree_ids, short_id_map))

    user_message = build_user_message(context_text, task_description)

    agent_result = agent_loop(
        system_prompt,
        user_message,
        tools,
        max_tokens=max_tokens,
        max_rounds=max_rounds,
    )

    return RunCallResult(
        created_page_ids=state.created_page_ids,
        dispatches=state.dispatches,
        moves=state.moves,
        phase1_page_ids=phase1_ids,
        agent_result=agent_result,
    )


def extract_loaded_page_ids(result: RunCallResult, db: DB) -> list[str]:
    """Extract full page IDs for LOAD_PAGE moves from phase 2 only."""
    phase1_set = set(result.phase1_page_ids)
    loaded = []
    for m in result.moves:
        if m.move_type == MoveType.LOAD_PAGE:
            assert isinstance(m.payload, LoadPagePayload)
            full_id = db.resolve_page_id(m.payload.page_id)
            if full_id and full_id not in phase1_set:
                loaded.append(full_id)
    return loaded


def moves_to_trace_data(
    moves: list[Move],
    created_page_ids: list[str],
) -> dict:
    """Build the trace data dict for a moves_executed event."""
    return {
        "moves": [
            {
                "type": m.move_type.value,
                **m.payload.model_dump(exclude_none=True, exclude_defaults=True),
            }
            for m in moves
        ],
        "created_page_ids": created_page_ids,
    }


REVIEW_SYSTEM_PROMPT = (
    "You are a research assistant completing a closing review of a call you just made "
    "in a collaborative research workspace. Be honest and specific in your self-assessment."
)


def print_page_ratings(review: dict, db: DB) -> None:
    ratings = review.get("page_ratings", [])
    if not ratings:
        return
    score_labels = {-1: "confusing", 0: "no help", 1: "helpful", 2: "very helpful"}
    for r in ratings:
        pid = r.get("page_id", "?")
        resolved = db.resolve_page_id(pid) if pid != "?" else None
        page_label = db.page_label(resolved or pid) if resolved else f"[{pid}]"
        score = r.get("score", "?")
        note = r.get("note", "")
        label = score_labels.get(score, str(score))
        print(f"  [page] {page_label} [{label}]: {note}")


def complete_call(
    call: Call, db: DB, summary: str, trace: CallTrace | None = None
) -> None:
    call.status = CallStatus.COMPLETE
    call.completed_at = datetime.now(UTC)
    call.result_summary = summary
    db.save_call(call)
    if trace:
        trace.save()


def run_closing_review(
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
            page = db.get_page(pid)
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

    try:
        user_message = build_user_message(context_text, review_task)
        review = structured_call(
            system_prompt=REVIEW_SYSTEM_PROMPT,
            user_message=user_message,
            response_model=ReviewResponse,
            max_tokens=2048,
        )
        if review and db:
            for r in review.get("page_ratings", []):
                pid = db.resolve_page_id(r.get("page_id", ""))
                score = r.get("score")
                if pid and isinstance(score, int):
                    db.save_page_rating(pid, call.id, score, r.get("note", ""))
        return review
    except Exception as e:
        status = getattr(e, "status_code", None)
        reason = f"HTTP {status}" if status else type(e).__name__
        print(f"  [review] Closing review skipped ({reason}) — continuing.")
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
