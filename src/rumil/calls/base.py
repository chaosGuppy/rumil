"""Base class hierarchy for call types."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from rumil.calls.common import (
    RunCallResult,
    _format_loaded_pages,
    _run_phase1,
    complete_call,
    extract_loaded_page_ids,
    format_moves_for_review,
    log_page_ratings,
    resolve_page_refs,
    run_agent_loop,
    run_closing_review,
)
from rumil.database import DB
from rumil.llm import build_system_prompt, build_user_message
from rumil.models import Call, CallStatus, CallType, MoveType
from rumil.moves.base import MoveState
from rumil.moves.registry import MOVES
from rumil.settings import get_settings
from rumil.tracing.trace_events import ContextBuiltEvent, ReviewCompleteEvent
from rumil.tracing.tracer import CallTrace

log = logging.getLogger(__name__)


class BaseCall(ABC):
    """Template for all call types. Subclasses override individual stages."""

    def __init__(
        self,
        question_id: str,
        call: Call,
        db: DB,
        *,
        broadcaster=None,
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

    async def run(self) -> None:
        await self._enter_running()
        await self.build_context()
        await self.create_pages()
        await self.closing_review()
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
        await complete_call(self.call, self.db, self.result_summary())


class SimpleCall(BaseCall):
    """Shared lifecycle for assess and ingest calls.

    Subclasses set self.context_text in build_context(), then call
    self._load_phase1_pages() to run the preliminary page-loading LLM call.
    create_pages() runs the main agent loop.
    """

    def __init__(
        self,
        question_id: str,
        call: Call,
        db: DB,
        *,
        broadcaster=None,
    ):
        super().__init__(question_id, call, db, broadcaster=broadcaster)
        self.result: RunCallResult = RunCallResult()
        self.phase1_ids: list[str] = []
        self.available_moves: list[MoveType] = list(MoveType)

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
        max_rounds = 1 if get_settings().is_smoke_test else 3
        system_prompt = build_system_prompt(self.call_type().value)
        tools = [MOVES[mt].bind(self.state) for mt in self.available_moves]
        user_message = build_user_message(
            self.context_text, self.task_description(),
        )

        agent_result = await run_agent_loop(
            system_prompt, user_message, tools,
            call_id=self.call.id,
            db=self.db,
            state=self.state,
            trace=self.trace,
            max_tokens=4096,
            max_rounds=max_rounds,
        )

        log.info(
            "create_pages complete: type=%s, pages_created=%d, "
            "dispatches=%d, moves=%d",
            self.call_type().value, len(self.state.created_page_ids),
            len(self.state.dispatches), len(self.state.moves),
        )
        self.result = RunCallResult(
            created_page_ids=self.state.created_page_ids,
            dispatches=self.state.dispatches,
            moves=self.state.moves,
            phase1_page_ids=self.phase1_ids,
            agent_result=agent_result,
        )

        phase2_loaded = await extract_loaded_page_ids(self.result, self.db)
        self.all_loaded_ids = list(
            dict.fromkeys(
                self.preloaded_ids + self.phase1_ids + phase2_loaded
            )
        )

    async def closing_review(self) -> None:
        review_context = format_moves_for_review(self.result.moves)
        review = await run_closing_review(
            self.call,
            review_context,
            self.context_text,
            self.all_loaded_ids,
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
