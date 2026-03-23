"""Base class hierarchy for call types."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from pydantic import BaseModel, Field

from rumil.calls.common import (
    RunCallResult,
    _format_loaded_pages,
    _prepare_tools,
    _run_phase1,
    mark_call_completed,
    extract_loaded_page_ids,
    format_moves_for_review,
    log_page_ratings,
    resolve_page_refs,
    run_agent_loop,
    run_closing_review,
)
from rumil.database import DB
from rumil.llm import (
    LLMExchangeMetadata,
    build_system_prompt,
    build_user_message,
    structured_call,
)
from rumil.models import Call, CallStage, CallStatus, CallType, MoveType
from rumil.moves.base import MoveState
from rumil.moves.registry import MOVES
from rumil.settings import get_settings
from rumil.tracing.trace_events import ContextBuiltEvent, ReviewCompleteEvent
from rumil.tracing.tracer import CallTrace

log = logging.getLogger(__name__)


class BaseCall(ABC):
    """Template for all call types. Subclasses override individual stages."""

    STAGES = (
        CallStage.BUILD_CONTEXT,
        CallStage.CREATE_PAGES,
        CallStage.CLOSING_REVIEW,
    )

    def __init__(
        self,
        question_id: str,
        call: Call,
        db: DB,
        *,
        broadcaster=None,
        up_to_stage: CallStage | None = None,
    ):
        self.question_id = question_id
        self.call = call
        self.db = db
        self.trace = CallTrace(call.id, db, broadcaster=broadcaster)
        self.state = MoveState(call, db)

        self.context_text: str = ""
        self.working_page_ids: list[str] = []
        self.preloaded_ids: list[str] = call.context_page_ids or []
        self.all_loaded_ids: list[str] = []
        self.review: dict = {}
        self.up_to_stage = up_to_stage

    async def run(self) -> None:
        await self._enter_running()
        for stage in self.STAGES:
            await getattr(self, stage.value)()
            if stage == self.up_to_stage:
                break
        await self._finalize()

    async def _enter_running(self) -> None:
        await self.db.update_call_status(self.call.id, CallStatus.RUNNING)

    @abstractmethod
    async def build_context(self) -> None:
        ...

    @abstractmethod
    async def create_pages(self) -> None:
        ...

    @abstractmethod
    async def closing_review(self) -> None:
        ...

    @abstractmethod
    def result_summary(self) -> str:
        ...

    async def _finalize(self) -> None:
        self.call.review_json = self.review
        await mark_call_completed(self.call, self.db, self.result_summary())


class FruitCheck(BaseModel):
    remaining_fruit: int = Field(
        description=(
            '0-10 integer: how much useful work remains on this scope. '
            '0 = nothing more to add; 1-2 = close to exhausted; '
            '3-4 = most angles covered; 5-6 = diminishing but real returns; '
            '7-8 = substantial work remains; 9-10 = barely started'
        )
    )
    brief_reasoning: str = Field(
        description='One sentence explaining why you chose this score'
    )


_FRUIT_CHECK_MESSAGE = (
    'Before continuing, rate how much useful work remains on this '
    'scope question. Consider what you have already contributed and what '
    'angles are left unexplored. Respond with remaining_fruit (0-10) and '
    'brief_reasoning. Do not call any tools — they will have no effect here.'
)


_CONTINUE_MESSAGE = (
    'Continue your work on this question. You have already made contributions '
    'in prior rounds (visible above). Focus on NEW angles, evidence, or '
    'sub-questions you have not yet covered.\n\n'
    'Question ID: `{question_id}`'
)


class SimpleCall(BaseCall):
    """Shared lifecycle for assess, ingest, and specialized scout calls.

    Subclasses set self.context_text in build_context(), then call
    self._load_phase1_pages() to run the preliminary page-loading LLM call.
    create_pages() runs the main agent loop with optional multi-round support
    controlled by scout_max_rounds and scout_fruit_threshold.
    """

    def __init__(
        self,
        question_id: str,
        call: Call,
        db: DB,
        *,
        broadcaster=None,
        up_to_stage: CallStage | None = None,
        scout_max_rounds: int = 1,
        scout_fruit_threshold: int = 4,
    ):
        super().__init__(question_id, call, db, broadcaster=broadcaster, up_to_stage=up_to_stage)
        self.result: RunCallResult = RunCallResult()
        self.phase1_ids: list[str] = []
        self.available_moves: list[MoveType] = list(MoveType)
        self.scout_max_rounds = scout_max_rounds
        self.scout_fruit_threshold = scout_fruit_threshold
        self.rounds_completed = 0
        self.last_fruit_score: int | None = None

    @abstractmethod
    def call_type(self) -> CallType:
        ...

    @abstractmethod
    def task_description(self) -> str:
        ...

    async def _load_phase1_pages(self) -> None:
        """Run the preliminary page-loading LLM call and append results to context.

        Call this at the end of build_context() after setting self.context_text.
        """
        system_prompt = build_system_prompt(self.call_type().value)
        self.phase1_ids = await _run_phase1(
            system_prompt, self.context_text, self.call.id,
            self.state, self.db, trace=self.trace,
        )
        if self.phase1_ids:
            extra_text = await _format_loaded_pages(self.phase1_ids, self.db)
            self.context_text += '\n\n## Loaded Pages\n\n' + extra_text

    async def create_pages(self) -> None:
        max_agent_rounds = 1 if get_settings().is_smoke_test else 3
        system_prompt = build_system_prompt(self.call_type().value)
        tools = [MOVES[mt].bind(self.state) for mt in self.available_moves]
        tool_defs, _ = _prepare_tools(tools)
        user_message = build_user_message(
            self.context_text, self.task_description(),
        )

        resume_messages: list[dict] = []
        agent_result = None

        for scout_round in range(self.scout_max_rounds):
            if scout_round > 0 and not await self.db.consume_budget(1):
                log.info(
                    'Budget exhausted, stopping scout session at round %d',
                    scout_round,
                )
                break

            if scout_round == 0:
                agent_result = await run_agent_loop(
                    system_prompt, user_message=user_message, tools=tools,
                    call_id=self.call.id,
                    db=self.db,
                    state=self.state,
                    trace=self.trace,
                    max_rounds=max_agent_rounds,
                    cache=self.scout_max_rounds > 1,
                )
            else:
                continue_msg = _CONTINUE_MESSAGE.format(
                    question_id=self.question_id,
                )
                resume_messages.append(
                    {'role': 'user', 'content': continue_msg}
                )
                agent_result = await run_agent_loop(
                    system_prompt, tools=tools,
                    call_id=self.call.id,
                    db=self.db,
                    state=self.state,
                    trace=self.trace,
                    messages=resume_messages,
                    max_rounds=max_agent_rounds,
                    cache=True,
                )

            self.rounds_completed += 1
            resume_messages = list(agent_result.messages)

            if scout_round < self.scout_max_rounds - 1:
                self.last_fruit_score = await self._run_fruit_check(
                    system_prompt, resume_messages, tool_defs,
                )
                if self.last_fruit_score <= self.scout_fruit_threshold:
                    log.info(
                        'Scout fruit (%d) <= threshold (%d), stopping after round %d',
                        self.last_fruit_score, self.scout_fruit_threshold,
                        scout_round + 1,
                    )
                    break

        log.info(
            'create_pages complete: type=%s, rounds=%d, pages_created=%d, '
            'dispatches=%d, moves=%d',
            self.call_type().value, self.rounds_completed,
            len(self.state.created_page_ids),
            len(self.state.dispatches), len(self.state.moves),
        )
        self.result = RunCallResult(
            created_page_ids=self.state.created_page_ids,
            dispatches=self.state.dispatches,
            moves=self.state.moves,
            phase1_page_ids=self.phase1_ids,
            agent_result=agent_result or RunCallResult().agent_result,
        )

        phase2_loaded = await extract_loaded_page_ids(self.result, self.db)
        self.all_loaded_ids = list(
            dict.fromkeys(
                self.preloaded_ids + self.phase1_ids + phase2_loaded
            )
        )

    async def _run_fruit_check(
        self,
        system_prompt: str,
        messages: list[dict],
        tool_defs: list[dict],
    ) -> int:
        """Lightweight fruit check sharing the agent's cache prefix."""
        check_messages = messages + [
            {'role': 'user', 'content': _FRUIT_CHECK_MESSAGE},
        ]
        meta = LLMExchangeMetadata(
            call_id=self.call.id, phase='fruit_check', trace=self.trace,
            user_message=_FRUIT_CHECK_MESSAGE,
        )
        result = await structured_call(
            system_prompt=system_prompt,
            response_model=FruitCheck,
            messages=check_messages,
            tools=tool_defs,
            metadata=meta,
            db=self.db,
            cache=True,
        )
        if result.data:
            score = result.data.get('remaining_fruit', 5)
            log.info(
                'Fruit check: score=%d, reasoning=%s',
                score, result.data.get('brief_reasoning', ''),
            )
            return score
        log.warning('Fruit check returned empty data, defaulting to 5')
        return 5

    async def closing_review(self) -> None:
        review_context = format_moves_for_review(self.result.moves)
        review = await run_closing_review(
            self.call,
            review_context,
            self.context_text,
            self.all_loaded_ids,
            self.result.created_page_ids,
            self.db,
            self.trace,
        )
        if review:
            self._log_review(review)
            await log_page_ratings(review, self.db)
            await self.trace.record(ReviewCompleteEvent(
                remaining_fruit=review.get("remaining_fruit"),
                confidence=review.get("confidence_in_output"),
            ))
        self.review = review or {}

    def _log_review(self, review: dict) -> None:
        log.info(
            "%s review: confidence=%s",
            self.call_type().value.capitalize(),
            review.get("confidence_in_output", "?"),
        )

    async def _record_context_built(
        self, *, source_page_id: str | None = None, scout_mode: str | None = None,
    ) -> None:
        await self.trace.record(ContextBuiltEvent(
            working_context_page_ids=await resolve_page_refs(
                self.working_page_ids, self.db,
            ),
            preloaded_page_ids=await resolve_page_refs(self.preloaded_ids, self.db),
            source_page_id=source_page_id,
            scout_mode=scout_mode,
        ))
