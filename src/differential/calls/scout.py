"""Scout call: find missing considerations on a question."""

import logging
from dataclasses import dataclass

from pydantic import BaseModel, Field

from differential.calls.common import (
    ReviewResponse,
    RunCallResult,
    _execute_review_links,
    _format_loaded_pages,
    _prepare_tools,
    _run_initial_page_loading,
    auto_unlink_unhelpful_pages,
    complete_call,
    extract_loaded_page_ids,
    format_moves_for_review,
    log_page_ratings,
    resolve_page_refs,
    run_agent_loop,
    run_call,
    run_closing_review,
)
from differential.context import (
    assemble_call_context,
    build_context_for_question,
    format_preloaded_pages,
)
from differential.database import DB
from differential.llm import (
    build_system_prompt,
    build_user_message,
    structured_call,
    LLMExchangeMetadata,
)
from differential.models import Call, CallStatus, CallType, MoveType, ScoutMode
from differential.moves.base import MoveState
from differential.moves.registry import MOVES
from differential.tracing.trace_events import ContextBuiltEvent, ReviewCompleteEvent
from differential.tracing.tracer import CallTrace
from differential.workspace_map import build_workspace_map

log = logging.getLogger(__name__)


_CONCRETE_INSTRUCTION = (
    '\n\n**Mode: CONCRETE**\n\n'
    'Your goal is considerations, sub-questions, and hypotheses that are as specific '
    'and falsifiable as possible. Concreteness means: named actors, specific timeframes, '
    'quantitative claims, named mechanisms, particular cases. A concrete claim should be '
    'possible to be clearly wrong about — that is what makes it valuable.\n\n'
    'Concrete scouts are expected to produce claims that subsequent investigation may '
    'refute. That is a feature, not a failure. Do not hedge your way back to vagueness.'
)


class FruitCheck(BaseModel):
    remaining_fruit: int = Field(
        description=(
            "0-10 integer: how much useful work remains on this scope. "
            "0 = nothing more to add; 1-2 = close to exhausted; "
            "3-4 = most angles covered; 5-6 = diminishing but real returns; "
            "7-8 = substantial work remains; 9-10 = barely started"
        )
    )
    brief_reasoning: str = Field(
        description="One sentence explaining why you chose this score"
    )


_FRUIT_CHECK_MESSAGE = (
    "Before continuing, rate how much useful scouting work remains on this "
    "scope question. Consider what you have already contributed and what "
    "angles are left unexplored. Respond with remaining_fruit (0-10) and "
    "brief_reasoning. Do not call any tools — they will have no effect here."
)


async def _run_fruit_check(
    system_prompt: str,
    agent_messages: list[dict],
    tool_defs: list[dict],
    *,
    call_id: str,
    db: DB,
    trace: CallTrace | None = None,
) -> int:
    """Run a lightweight fruit check sharing the agent's cache prefix.

    Appends a fruit-check user message to a *copy* of agent_messages and calls
    structured_call with the same system prompt, tools, and tool_choice=none.
    Returns the remaining_fruit score.
    """
    check_messages = list(agent_messages) + [
        {"role": "user", "content": _FRUIT_CHECK_MESSAGE},
    ]
    meta = LLMExchangeMetadata(
        call_id=call_id, phase="fruit_check", trace=trace,
        user_message=_FRUIT_CHECK_MESSAGE,
    )
    result = await structured_call(
        system_prompt=system_prompt,
        response_model=FruitCheck,
        messages=check_messages,
        tools=tool_defs,
        max_tokens=256,
        metadata=meta,
        db=db,
        cache=True,
    )
    if result.data:
        score = result.data.get("remaining_fruit", 5)
        log.info(
            "Fruit check: score=%d, reasoning=%s",
            score, result.data.get("brief_reasoning", ""),
        )
        return score
    log.warning("Fruit check returned empty data, defaulting to 5")
    return 5


async def run_scout(
    question_id: str,
    call: Call,
    db: DB,
    mode: ScoutMode = ScoutMode.ABSTRACT,
    broadcaster=None,
    max_rounds: int | None = None,
    fruit_threshold: int | None = None,
) -> tuple[RunCallResult, dict]:
    """Run a Scout call on a question.

    Returns (run_call_result, review_dict).
    """
    call.call_params = {"mode": mode.value}
    if max_rounds is not None:
        call.call_params["max_rounds"] = max_rounds
    if fruit_threshold is not None:
        call.call_params["fruit_threshold"] = fruit_threshold
    trace = CallTrace(call.id, db, broadcaster=broadcaster)
    log.info(
        "Scout starting: call=%s, question=%s, mode=%s",
        call.id[:8], question_id[:8], mode.value,
    )

    working_context, working_page_ids = await build_context_for_question(
        question_id, db,
    )

    preloaded = call.context_page_ids or []
    if preloaded:
        working_context += await format_preloaded_pages(preloaded, db)

    await trace.record(ContextBuiltEvent(
        working_context_page_ids=await resolve_page_refs(working_page_ids, db),
        preloaded_page_ids=await resolve_page_refs(preloaded, db),
        scout_mode=mode.value,
    ))

    mode_instruction = _CONCRETE_INSTRUCTION if mode == ScoutMode.CONCRETE else ''
    task = (
        f"Scout for missing considerations on this question.{mode_instruction}\n\n"
        f"Question ID (use this when linking considerations): `{question_id}`"
    )

    await db.update_call_status(call.id, CallStatus.RUNNING)
    result = await run_call(
        CallType.SCOUT, task, working_context, call, db, trace=trace,
    )
    phase2_loaded = await extract_loaded_page_ids(result, db)

    all_loaded_summaries = list(result.loaded_page_summaries)
    for pid in phase2_loaded:
        page = await db.get_page(pid)
        if page:
            all_loaded_summaries.append((pid, page.summary))
    for pid in preloaded:
        page = await db.get_page(pid)
        if page and not any(s[0] == pid for s in all_loaded_summaries):
            all_loaded_summaries.append((pid, page.summary))

    review_context = format_moves_for_review(result.moves)
    review = await run_closing_review(
        call, review_context, working_context, all_loaded_summaries, db, trace,
        scope_question_id=question_id,
    )
    remaining_fruit = 5
    if not review.error:
        remaining_fruit = review.data.get("remaining_fruit", 5)
        log.info(
            "Scout review: remaining_fruit=%d, confidence=%s",
            remaining_fruit, review.data.get("confidence_in_output", "?"),
        )
        await log_page_ratings(review.data, db)
        await trace.record(ReviewCompleteEvent(
            remaining_fruit=remaining_fruit,
            confidence=review.data.get("confidence_in_output"),
        ))
        await auto_unlink_unhelpful_pages(review.data, call.scope_page_id, db)

    call.review_json = review.data
    log.info(
        "Scout complete: call=%s, pages_created=%d, fruit=%d",
        call.id[:8], len(result.created_page_ids), remaining_fruit,
    )
    await complete_call(
        call,
        db,
        f"Scout complete. Created {len(result.created_page_ids)} pages. Remaining fruit: {remaining_fruit}",
    )
    return result, review.data


def _resolve_round_mode(mode: ScoutMode, round_index: int) -> ScoutMode:
    """Resolve the effective mode for a given scout round."""
    if mode == ScoutMode.ALTERNATE:
        return ScoutMode.ABSTRACT if round_index % 2 == 0 else ScoutMode.CONCRETE
    return mode


_CONTINUE_TEMPLATE = (
    'Continue scouting this question. You have already made contributions in '
    'prior rounds (visible above). Focus on NEW angles, evidence, or '
    'sub-questions you have not yet covered.{mode_instruction}\n\n'
    'Question ID: `{question_id}`'
)

_REVIEW_INSTRUCTION = (
    'You have finished scouting. Now perform your closing review. '
    'Do not call any tools — they will have no effect here.\n\n'
    'For each page that was loaded into your context (listed in your working '
    'context), rate how helpful it was. For helpful pages (score 1 or 2), '
    'include a link to connect it to the scope question.\n\n'
    'Scope question ID: `{question_id}`'
)


@dataclass
class _SessionContext:
    """Everything the scout round loop needs, built once up front."""

    system_prompt: str
    user_message: str
    tools: list
    tool_defs: list[dict]
    state: MoveState
    trace: CallTrace
    phase1_summaries: list[tuple[str, str]]
    preloaded_ids: list[str]


async def _build_session_context(
    question_id: str,
    call: Call,
    db: DB,
    *,
    mode: ScoutMode,
    context_page_ids: list[str] | None,
    broadcaster=None,
) -> _SessionContext:
    """Build all context needed for a scout session.

    Runs phase-1 page loading, assembles phase-2 context, and prepares tools.
    """
    trace = CallTrace(call.id, db, broadcaster=broadcaster)
    state = MoveState(call, db)
    system_prompt = build_system_prompt(CallType.SCOUT.value)

    working_context, working_page_ids = await build_context_for_question(
        question_id, db,
    )

    preloaded = context_page_ids or []
    if preloaded:
        working_context += await format_preloaded_pages(preloaded, db)

    await trace.record(ContextBuiltEvent(
        working_context_page_ids=await resolve_page_refs(working_page_ids, db),
        preloaded_page_ids=await resolve_page_refs(preloaded, db),
        scout_mode=mode.value,
    ))

    last_call_time = await db.get_last_successful_call_time(
        CallType.SCOUT, question_id,
    )

    phase1_ids: list[str] = []
    if last_call_time:
        filtered_map, filtered_ids = await build_workspace_map(
            db, created_after=last_call_time,
        )
        if filtered_ids:
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

    extra_pages_text: str | None = None
    phase1_summaries: list[tuple[str, str]] = []
    if phase1_ids:
        extra_pages_text, phase1_summaries = await _format_loaded_pages(
            phase1_ids, db,
        )

    workspace_map, _ = await build_workspace_map(db)
    phase2_context = assemble_call_context(
        working_context, workspace_map=workspace_map,
        extra_pages_text=extra_pages_text,
    )

    tools = [MOVES[mt].bind(state) for mt in MoveType]
    tool_defs, _ = _prepare_tools(tools)

    round_mode = _resolve_round_mode(mode, 0)
    mode_instruction = _CONCRETE_INSTRUCTION if round_mode == ScoutMode.CONCRETE else ''
    task = (
        f"Scout for missing considerations on this question.{mode_instruction}\n\n"
        f"Question ID (use this when linking considerations): `{question_id}`"
    )
    user_message = build_user_message(phase2_context, task)

    return _SessionContext(
        system_prompt=system_prompt,
        user_message=user_message,
        tools=tools,
        tool_defs=tool_defs,
        state=state,
        trace=trace,
        phase1_summaries=phase1_summaries,
        preloaded_ids=preloaded,
    )


async def _collect_all_loaded_summaries(
    state: MoveState,
    phase1_summaries: list[tuple[str, str]],
    preloaded_ids: list[str],
    db: DB,
) -> list[tuple[str, str]]:
    """Gather page summaries from phase 1, agent moves, and preloaded pages."""
    from differential.moves.load_page import LoadPagePayload

    summaries = list(phase1_summaries)
    seen = {pid for pid, _ in summaries}

    for m in state.moves:
        if m.move_type == MoveType.LOAD_PAGE:
            assert isinstance(m.payload, LoadPagePayload)
            full_id = await db.resolve_page_id(m.payload.page_id)
            if full_id and full_id not in seen:
                page = await db.get_page(full_id)
                if page:
                    summaries.append((full_id, page.summary))
                    seen.add(full_id)

    for pid in preloaded_ids:
        if pid not in seen:
            page = await db.get_page(pid)
            if page:
                summaries.append((pid, page.summary))
                seen.add(pid)

    return summaries


async def _run_session_review(
    question_id: str,
    call: Call,
    db: DB,
    *,
    system_prompt: str,
    tool_defs: list[dict],
    resume_messages: list[dict],
    loaded_summaries: list[tuple[str, str]],
    trace: CallTrace,
) -> dict:
    """Run the closing review for a scout session.

    Appends a review instruction to the agent's conversation and calls
    structured_call with the same system prompt and tools for cache reuse.
    Processes page ratings, links, and auto-unlinking.
    Returns the review data dict.
    """
    page_rating_note = ""
    if loaded_summaries:
        page_lines = [
            f'  - `{pid[:8]}`: "{summary[:120]}"'
            for pid, summary in loaded_summaries
        ]
        scope_note = (
            '\n\nFor each page you rate as helpful (1) or very helpful (2), '
            'include a link in the `links` field to connect it to the scope '
            f'question `{question_id[:8]}`. Use link_type "consideration" '
            'for claims (with strength), "child_question" for '
            'sub-questions, or "related" for other page types.'
        )
        page_rating_note = (
            '\n\nThe following pages were loaded into your context:\n'
            + '\n'.join(page_lines)
            + '\n\nPlease include a rating for each in your page_ratings. '
            'Scores: -1 = actively confusing, 0 = didn\'t help, '
            '1 = helped, 2 = extremely helpful.'
            + scope_note
        )

    review_instruction = (
        _REVIEW_INSTRUCTION.format(question_id=question_id)
        + page_rating_note
    )
    review_messages = list(resume_messages) + [
        {"role": "user", "content": review_instruction},
    ]
    meta = LLMExchangeMetadata(
        call_id=call.id, phase="closing_review", trace=trace,
        user_message=review_instruction,
    )
    review_result = await structured_call(
        system_prompt=system_prompt,
        response_model=ReviewResponse,
        messages=review_messages,
        tools=tool_defs,
        max_tokens=4096,
        metadata=meta,
        db=db,
        cache=True,
    )
    review_data = review_result.data or {}

    if review_data:
        log.info(
            "Scout session review: confidence=%s",
            review_data.get("confidence_in_output", "?"),
        )
        await log_page_ratings(review_data, db)

        review_links = review_data.get("links", [])
        if review_links:
            await _execute_review_links(review_links, question_id, db)

        for r in review_data.get("page_ratings", []):
            pid = await db.resolve_page_id(r.get("page_id", ""))
            score = r.get("score")
            if pid and isinstance(score, int):
                await db.save_page_rating(pid, call.id, score, r.get("note", ""))

        await auto_unlink_unhelpful_pages(review_data, call.scope_page_id, db)

    call.review_json = review_data
    return review_data


async def run_scout_session(
    question_id: str,
    call: Call,
    db: DB,
    *,
    max_rounds: int,
    fruit_threshold: int,
    mode: ScoutMode = ScoutMode.ALTERNATE,
    context_page_ids: list[str] | None = None,
    broadcaster=None,
) -> int:
    """Cache-aware multi-round scout session.

    Builds context once, resumes the agent conversation across rounds,
    uses lightweight fruit checks, and runs linking once at the end.
    Returns (rounds_completed, created_page_ids).
    """
    await db.update_call_status(call.id, CallStatus.RUNNING)

    ctx = await _build_session_context(
        question_id, call, db,
        mode=mode, context_page_ids=context_page_ids, broadcaster=broadcaster,
    )

    resume_messages: list[dict] = []
    rounds_completed = 0
    last_fruit_score: int | None = None

    for i in range(max_rounds):
        if not await db.consume_budget(1):
            log.info("Budget exhausted, stopping scout session at round %d", i)
            break

        round_mode = _resolve_round_mode(mode, i)

        if i == 0:
            agent_result = await run_agent_loop(
                ctx.system_prompt,
                user_message=ctx.user_message,
                tools=ctx.tools,
                call_id=call.id,
                db=db,
                state=ctx.state,
                trace=ctx.trace,
                cache=True,
            )
        else:
            mode_instruction = (
                _CONCRETE_INSTRUCTION if round_mode == ScoutMode.CONCRETE else ''
            )
            continue_msg = _CONTINUE_TEMPLATE.format(
                mode_instruction=mode_instruction, question_id=question_id,
            )
            resume_messages.append(
                {"role": "user", "content": continue_msg}
            )
            agent_result = await run_agent_loop(
                ctx.system_prompt,
                tools=ctx.tools,
                call_id=call.id,
                db=db,
                state=ctx.state,
                trace=ctx.trace,
                messages=resume_messages,
                cache=True,
            )

        rounds_completed += 1
        resume_messages = list(agent_result.messages)

        last_fruit_score = await _run_fruit_check(
            ctx.system_prompt, resume_messages, ctx.tool_defs,
            call_id=call.id, db=db, trace=ctx.trace,
        )
        if last_fruit_score <= fruit_threshold:
            log.info(
                "Scout fruit (%d) <= threshold (%d), stopping after round %d",
                last_fruit_score, fruit_threshold, i + 1,
            )
            break

    if resume_messages:
        assert last_fruit_score is not None
        loaded_summaries = await _collect_all_loaded_summaries(
            ctx.state, ctx.phase1_summaries, ctx.preloaded_ids, db,
        )
        review_data = await _run_session_review(
            question_id, call, db,
            system_prompt=ctx.system_prompt,
            tool_defs=ctx.tool_defs,
            resume_messages=resume_messages,
            loaded_summaries=loaded_summaries,
            trace=ctx.trace,
        )
        await ctx.trace.record(ReviewCompleteEvent(
            remaining_fruit=last_fruit_score,
            confidence=review_data.get("confidence_in_output"),
        ))

    log.info(
        "Scout session complete: call=%s, rounds=%d, pages_created=%d",
        call.id[:8], rounds_completed, len(ctx.state.created_page_ids),
    )
    await complete_call(
        call, db,
        f"Scout session complete. {rounds_completed} rounds, "
        f"{len(ctx.state.created_page_ids)} pages created.",
    )
    return rounds_completed
