"""Scout call: find missing considerations on a question."""

import logging

from pydantic import BaseModel, Field

from rumil.calls.base import BaseCall
from rumil.calls.common import (
    ReviewResponse,
    _prepare_tools,
    complete_call,
    log_page_ratings,
    resolve_page_refs,
    run_agent_loop,
    run_single_call,
)
from rumil.context import (
    assemble_call_context,
    build_embedding_based_context,
    format_page,
    format_preloaded_pages,
    format_question_for_scout,
)
from rumil.database import DB
from rumil.llm import (
    build_system_prompt,
    build_user_message,
    structured_call,
    LLMExchangeMetadata,
)
from rumil.models import Call, CallStatus, CallType, MoveType, ScoutMode
from rumil.moves.base import MoveState
from rumil.moves.registry import MOVES
from rumil.tracing.trace_events import ContextBuiltEvent, ReviewCompleteEvent
from rumil.tracing.tracer import CallTrace
from rumil.workspace_map import build_workspace_map

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


_LINKING_TASK = (
    'Review the workspace and link relevant existing pages to the scope question.\n\n'
    'For each link, specify a role:\n'
    '- **direct**: "Now I know X, I can immediately update my answer." The page '
    'directly bears on the answer to the scope question — it is evidence, a '
    'counter-argument, or a partial answer.\n'
    '- **structural**: "Now I know X, I know better what evidence and angles to '
    'consider." The page frames the investigation — it indicates what to look '
    'for, how to decompose the question, or what dimensions matter.\n\n'
    'Link claims as considerations and sub-questions as child questions.\n\n'
    'Be discerning. Only link pages that genuinely bear on this question — '
    'tangential or weakly related pages should not be linked. '
    'Do not duplicate any links already shown above. '
    'Create no more than 6 new links, and fewer if fewer are warranted — '
    'do not force links just to fill a quota.\n\n'
    'Scope question ID: `{question_id}`'
)


async def link_new_pages(
    question_id: str,
    call: Call,
    db: DB,
    state: MoveState,
    trace: CallTrace,
) -> None:
    """Single LLM call that reviews the workspace and creates direct/structural links.

    Uses only LINK_CONSIDERATION and LINK_CHILD_QUESTION tools with role fields.
    Free (not counted against budget).
    """
    question = await db.get_page(question_id)
    if not question:
        return

    workspace_map, _ = await build_workspace_map(db)
    question_text = await format_page(question)
    existing_links = await _build_link_inventory(question_id, db)
    working_context = (
        workspace_map + '\n\n---\n\n'
        '# Scope Question\n\n' + question_text
        + '\n\n' + existing_links
    )

    linking_tools = [
        MOVES[MoveType.LINK_CONSIDERATION].bind(state),
        MOVES[MoveType.LINK_CHILD_QUESTION].bind(state),
    ]
    task = _LINKING_TASK.format(question_id=question_id)
    system_prompt = build_system_prompt(CallType.SCOUT.value)
    user_message = build_user_message(working_context, task)

    await run_single_call(
        system_prompt=system_prompt,
        user_message=user_message,
        tools=linking_tools,
        call_id=call.id,
        phase="link_new_pages",
        db=db,
        state=state,
        trace=trace,
        max_tokens=2048,
    )


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

_LINK_REVIEW_INSTRUCTION = (
    'You have finished scouting. Before your self-assessment, review the '
    'links on the scope question.\n\n'
    'For each link below, decide whether it should stay as-is, have its '
    'role changed (direct ↔ structural), or be removed entirely.\n\n'
    '- **direct**: the linked page directly bears on the answer.\n'
    '- **structural**: the linked page frames what evidence/angles to explore.\n'
    '- **remove**: the link is no longer relevant or useful.\n\n'
    'Use `change_link_role` to switch a link between direct and structural. '
    'Use `remove_link` to delete a link that should not exist. '
    'Leave links alone if they are already correct.\n\n'
    '{link_inventory}\n\n'
    'Scope question ID: `{question_id}`'
)

_SELF_ASSESSMENT_INSTRUCTION = (
    'Now provide your self-assessment. Do not call any tools — they will '
    'have no effect here.\n\n'
    'Scope question ID: `{question_id}`'
)


async def _collect_all_loaded_summaries(
    state: MoveState,
    phase1_summaries: list[tuple[str, str]],
    preloaded_ids: list[str],
    db: DB,
) -> list[tuple[str, str]]:
    """Gather page summaries from phase 1, agent moves, and preloaded pages."""
    from rumil.moves.load_page import LoadPagePayload

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


async def _build_link_inventory(question_id: str, db: DB) -> str:
    """Build a text inventory of all links to/from the scope question."""
    considerations = await db.get_considerations_for_question(question_id)
    children_with_links = await db.get_child_questions_with_links(question_id)

    if not considerations and not children_with_links:
        return "No existing links on the scope question."

    lines = ["### Current Links"]
    for page, link in considerations:
        lines.append(
            f"- [{link.role.value}] consideration: "
            f'"{page.summary}" '
            f"(strength {link.strength:.1f}, link_id: `{link.id}`)"
        )
    for page, link in children_with_links:
        lines.append(
            f"- [{link.role.value}] child_question: "
            f'"{page.summary}" '
            f"(link_id: `{link.id}`)"
        )
    return "\n".join(lines)


class ScoutCall(BaseCall):
    """Multi-round scout session with fruit checking."""

    def __init__(
        self,
        question_id: str,
        call: Call,
        db: DB,
        *,
        max_rounds: int,
        fruit_threshold: int,
        mode: ScoutMode = ScoutMode.ALTERNATE,
        context_page_ids: list[str] | None = None,
        broadcaster=None,
    ):
        super().__init__(question_id, call, db, broadcaster=broadcaster)
        self.max_rounds = max_rounds
        self.fruit_threshold = fruit_threshold
        self.mode = mode
        self.context_page_ids = context_page_ids
        self.preloaded_ids = context_page_ids or []

        self.resume_messages: list[dict] = []
        self.rounds_completed = 0
        self.last_fruit_score: int | None = None

        self.system_prompt: str = ""
        self.user_message: str = ""
        self.tools: list = []
        self.tool_defs: list[dict] = []

    async def _enter_running(self) -> None:
        self.call.call_params = {
            "mode": self.mode.value,
            "max_rounds": self.max_rounds,
            "fruit_threshold": self.fruit_threshold,
        }
        await self.db.update_call_status(
            self.call.id, CallStatus.RUNNING, call_params=self.call.call_params,
        )

    async def build_context(self) -> None:
        await link_new_pages(
            self.question_id, self.call, self.db, self.state, self.trace,
        )

        working_context, self.working_page_ids = await format_question_for_scout(
            self.question_id, self.db,
        )

        if self.preloaded_ids:
            working_context += await format_preloaded_pages(self.preloaded_ids, self.db)

        await self.trace.record(ContextBuiltEvent(
            working_context_page_ids=await resolve_page_refs(
                self.working_page_ids, self.db,
            ),
            preloaded_page_ids=await resolve_page_refs(self.preloaded_ids, self.db),
            scout_mode=self.mode.value,
        ))

        workspace_map, _ = await build_workspace_map(self.db)
        phase2_context = assemble_call_context(
            working_context, workspace_map=workspace_map,
        )

        self.tools = [MOVES[mt].bind(self.state) for mt in MoveType]
        self.tool_defs, _ = _prepare_tools(self.tools)

        self.system_prompt = build_system_prompt(CallType.SCOUT.value)

        round_mode = _resolve_round_mode(self.mode, 0)
        mode_instruction = (
            _CONCRETE_INSTRUCTION if round_mode == ScoutMode.CONCRETE else ''
        )
        task = (
            f"Scout for missing considerations on this question.{mode_instruction}\n\n"
            f"Question ID (use this when linking considerations): "
            f"`{self.question_id}`"
        )
        self.user_message = build_user_message(phase2_context, task)

    async def create_pages(self) -> None:
        for i in range(self.max_rounds):
            if not await self.db.consume_budget(1):
                log.info(
                    "Budget exhausted, stopping scout session at round %d", i,
                )
                break

            round_mode = _resolve_round_mode(self.mode, i)

            if i == 0:
                agent_result = await run_agent_loop(
                    self.system_prompt,
                    user_message=self.user_message,
                    tools=self.tools,
                    call_id=self.call.id,
                    db=self.db,
                    state=self.state,
                    trace=self.trace,
                    cache=True,
                )
            else:
                mode_instruction = (
                    _CONCRETE_INSTRUCTION
                    if round_mode == ScoutMode.CONCRETE else ''
                )
                continue_msg = _CONTINUE_TEMPLATE.format(
                    mode_instruction=mode_instruction,
                    question_id=self.question_id,
                )
                self.resume_messages.append(
                    {"role": "user", "content": continue_msg}
                )
                agent_result = await run_agent_loop(
                    self.system_prompt,
                    tools=self.tools,
                    call_id=self.call.id,
                    db=self.db,
                    state=self.state,
                    trace=self.trace,
                    messages=self.resume_messages,
                    cache=True,
                )

            self.rounds_completed += 1
            self.resume_messages = list(agent_result.messages)

            self.last_fruit_score = await self.run_fruit_check()
            if self.last_fruit_score <= self.fruit_threshold:
                log.info(
                    "Scout fruit (%d) <= threshold (%d), stopping after round %d",
                    self.last_fruit_score, self.fruit_threshold, i + 1,
                )
                break

    async def run_fruit_check(self) -> int:
        """Run a lightweight fruit check sharing the agent's cache prefix."""
        check_messages = list(self.resume_messages) + [
            {"role": "user", "content": _FRUIT_CHECK_MESSAGE},
        ]
        meta = LLMExchangeMetadata(
            call_id=self.call.id, phase="fruit_check", trace=self.trace,
            user_message=_FRUIT_CHECK_MESSAGE,
        )
        result = await structured_call(
            system_prompt=self.system_prompt,
            response_model=FruitCheck,
            messages=check_messages,
            tools=self.tool_defs,
            max_tokens=256,
            metadata=meta,
            db=self.db,
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

    async def closing_review(self) -> None:
        if not self.resume_messages:
            return

        assert self.last_fruit_score is not None
        loaded_summaries = await _collect_all_loaded_summaries(
            self.state, [], self.preloaded_ids, self.db,
        )
        self.review = await self.run_session_review(loaded_summaries)
        await self.trace.record(ReviewCompleteEvent(
            remaining_fruit=self.last_fruit_score,
            confidence=self.review.get("confidence_in_output"),
        ))

    async def run_session_review(
        self, loaded_summaries: list[tuple[str, str]],
    ) -> dict:
        """Two-phase closing: link modification then self-assessment."""
        link_inventory = await _build_link_inventory(self.question_id, self.db)
        link_review_msg = _LINK_REVIEW_INSTRUCTION.format(
            link_inventory=link_inventory, question_id=self.question_id,
        )
        link_messages = list(self.resume_messages) + [
            {"role": "user", "content": link_review_msg},
        ]

        link_tools = [MOVES[mt].bind(self.state) for mt in MoveType]

        link_result = await run_single_call(
            self.system_prompt,
            tools=link_tools,
            call_id=self.call.id,
            phase="link_review",
            db=self.db,
            state=self.state,
            trace=self.trace,
            messages=link_messages,
            cache=True,
        )
        post_link_messages = list(link_result.messages)

        return await self._self_assessment(post_link_messages, loaded_summaries)

    async def _self_assessment(
        self,
        prior_messages: list[dict],
        loaded_summaries: list[tuple[str, str]],
    ) -> dict:
        """Structured self-assessment appended to a message history.

        Shared by the default two-phase review and the embedding variant's
        single-phase review.
        """
        page_rating_note = ""
        if loaded_summaries:
            page_lines = [
                f'  - `{pid[:8]}`: "{summary[:120]}"'
                for pid, summary in loaded_summaries
            ]
            page_rating_note = (
                '\n\nThe following pages were loaded into your context:\n'
                + '\n'.join(page_lines)
                + '\n\nPlease include a rating for each in your page_ratings. '
                'Scores: -1 = actively confusing, 0 = didn\'t help, '
                '1 = helped, 2 = extremely helpful.'
            )

        page_summary_note = ""
        if self.state.created_page_ids:
            created_lines = []
            for pid in self.state.created_page_ids:
                page = await self.db.get_page(pid)
                if page:
                    created_lines.append(f'  - `{pid[:8]}`: "{page.summary[:120]}"')
            if created_lines:
                page_summary_note = (
                    '\n\nYou created the following pages during this call:\n'
                    + '\n'.join(created_lines)
                    + '\n\nFor each, provide a summary_short (~30 words, fully self-contained) '
                    'and a summary_medium (~200 words, fully self-contained) in your page_summaries. '
                    'These will be read by other LLM instances with no prior context, so do not '
                    'assume any background knowledge.'
                )

        assessment_msg = (
            _SELF_ASSESSMENT_INSTRUCTION.format(question_id=self.question_id)
            + page_rating_note
            + page_summary_note
        )
        assessment_messages = prior_messages + [
            {"role": "user", "content": assessment_msg},
        ]
        meta = LLMExchangeMetadata(
            call_id=self.call.id, phase="closing_review", trace=self.trace,
            user_message=assessment_msg,
        )
        review_result = await structured_call(
            system_prompt=self.system_prompt,
            response_model=ReviewResponse,
            messages=assessment_messages,
            tools=self.tool_defs,
            max_tokens=8192,
            metadata=meta,
            db=self.db,
            cache=True,
        )
        review_data = review_result.data or {}

        if review_data:
            log.info(
                "Scout session review: confidence=%s",
                review_data.get("confidence_in_output", "?"),
            )
            await log_page_ratings(review_data, self.db)

            for r in review_data.get("page_ratings", []):
                pid = await self.db.resolve_page_id(r.get("page_id", ""))
                score = r.get("score")
                if pid and isinstance(score, int):
                    await self.db.save_page_rating(
                        pid, self.call.id, score, r.get("note", ""),
                    )
            for s in review_data.get("page_summaries", []):
                pid = await self.db.resolve_page_id(s.get("page_id", ""))
                if pid:
                    await self.db.update_page_summaries(
                        pid,
                        s.get("summary_short", ""),
                        s.get("summary_medium", ""),
                    )

        self.call.review_json = review_data
        return review_data

    def result_summary(self) -> str:
        return (
            f"Scout session complete. {self.rounds_completed} rounds, "
            f"{len(self.state.created_page_ids)} pages created."
        )

    async def _finalize(self) -> None:
        self.call.review_json = self.review
        await complete_call(self.call, self.db, self.result_summary())


class EmbeddingScoutCall(ScoutCall):
    """Scout call that builds context via embedding similarity search.

    Uses embedding search as the sole context source (no link_new_pages,
    no phase1 page loading). Closing review uses the simple single-call
    variant (no link review phase).
    """

    async def run_session_review(
        self, loaded_summaries: list[tuple[str, str]],
    ) -> dict:
        """Skip link review, go straight to self-assessment."""
        return await self._self_assessment(
            list(self.resume_messages), loaded_summaries,
        )

    async def build_context(self) -> None:
        question = await self.db.get_page(self.question_id)
        query = question.summary if question else self.question_id
        emb_result = await build_embedding_based_context(query, self.db)
        self.working_page_ids = emb_result.page_ids

        await self.trace.record(ContextBuiltEvent(
            working_context_page_ids=await resolve_page_refs(
                self.working_page_ids, self.db,
            ),
            preloaded_page_ids=[],
            scout_mode=self.mode.value,
        ))

        workspace_map, _ = await build_workspace_map(self.db)
        self.context_text = assemble_call_context(
            emb_result.context_text, workspace_map=workspace_map,
        )

        self.tools = [MOVES[mt].bind(self.state) for mt in MoveType]
        self.tool_defs, _ = _prepare_tools(self.tools)

        self.system_prompt = build_system_prompt(CallType.SCOUT.value)

        round_mode = _resolve_round_mode(self.mode, 0)
        mode_instruction = (
            _CONCRETE_INSTRUCTION if round_mode == ScoutMode.CONCRETE else ''
        )
        task = (
            f'Scout for missing considerations on this question.{mode_instruction}\n\n'
            f'Question ID (use this when linking considerations): '
            f'`{self.question_id}`'
        )
        self.user_message = build_user_message(self.context_text, task)


