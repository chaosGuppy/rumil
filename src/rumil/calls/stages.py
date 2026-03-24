"""Composition-based call abstraction: data types, stage ABCs, and CallRunner."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import ClassVar

from rumil.calls.common import mark_call_completed
from rumil.database import DB
from rumil.models import Call, CallStage, CallStatus, CallType, Dispatch, Move, MoveType
from rumil.move_presets import get_moves_for_call
from rumil.moves.base import MoveState
from rumil.tracing.tracer import CallTrace

log = logging.getLogger(__name__)


@dataclass
class CallInfra:
    """Shared infrastructure passed to all stages."""

    question_id: str
    call: Call
    db: DB
    trace: CallTrace
    state: MoveState


@dataclass
class ContextResult:
    """Output of the context-building stage."""

    context_text: str
    working_page_ids: list[str]
    preloaded_ids: Sequence[str] = field(default_factory=list)
    phase1_ids: list[str] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)


@dataclass
class CreationResult:
    """Output of the page-creation stage."""

    created_page_ids: list[str]
    moves: list[Move]
    all_loaded_ids: list[str]
    dispatches: list[Dispatch] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)
    last_fruit_score: int | None = None
    rounds_completed: int = 0


class ContextBuilder(ABC):
    @abstractmethod
    async def build_context(self, infra: CallInfra) -> ContextResult: ...


class PageCreator(ABC):
    @abstractmethod
    async def create_pages(
        self,
        infra: CallInfra,
        context: ContextResult,
    ) -> CreationResult: ...


class ClosingReviewer(ABC):
    @abstractmethod
    async def closing_review(
        self,
        infra: CallInfra,
        context: ContextResult,
        creation: CreationResult,
    ) -> None: ...


class CallRunner(ABC):
    """Base class for all call types using composition over inheritance.

    Subclasses set class-level stage class attributes and override
    task_description(). The CallRunner orchestrates the three stages.
    """

    context_builder_cls: ClassVar[type[ContextBuilder]]
    page_creator_cls: ClassVar[type[PageCreator]]
    closing_reviewer_cls: ClassVar[type[ClosingReviewer]]
    call_type: ClassVar[CallType]
    available_moves: ClassVar[Sequence[MoveType] | None] = None

    def __init__(
        self,
        question_id: str,
        call: Call,
        db: DB,
        *,
        broadcaster=None,
        up_to_stage: CallStage | None = None,
        max_rounds: int = 1,
        fruit_threshold: int = 4,
    ):
        self.infra = CallInfra(
            question_id=question_id,
            call=call,
            db=db,
            trace=CallTrace(call.id, db, broadcaster=broadcaster),
            state=MoveState(call, db),
        )
        self.up_to_stage = up_to_stage
        self._max_rounds = max_rounds
        self._fruit_threshold = fruit_threshold
        self.context_result: ContextResult | None = None
        self.creation_result: CreationResult | None = None
        if max_rounds > 1:
            call.call_params = {
                **(call.call_params or {}),
                "max_rounds": max_rounds,
                "fruit_threshold": fruit_threshold,
            }

        self.context_builder = self._make_context_builder()
        self.page_creator = self._make_page_creator()
        self.closing_reviewer = self._make_closing_reviewer()

    def _make_context_builder(self) -> ContextBuilder:
        return self.context_builder_cls()

    def _make_page_creator(self) -> PageCreator:
        return self.page_creator_cls()

    def _make_closing_reviewer(self) -> ClosingReviewer:
        return self.closing_reviewer_cls()

    def _resolve_available_moves(self) -> Sequence[MoveType] | None:
        """Return moves from the active preset, falling back to class-level available_moves."""
        preset_moves = get_moves_for_call(self.call_type)
        if preset_moves is not None:
            return preset_moves
        return self.available_moves

    @property
    def review(self) -> dict:
        """Access the call's review_json for backward compatibility."""
        return self.infra.call.review_json or {}

    @property
    def result(self) -> CreationResult:
        """Access the creation result for backward compatibility."""
        if self.creation_result is not None:
            return self.creation_result
        return CreationResult(created_page_ids=[], moves=[], all_loaded_ids=[])

    @abstractmethod
    def task_description(self) -> str: ...

    async def run(self) -> None:
        await self.infra.db.update_call_status(
            self.infra.call.id,
            CallStatus.RUNNING,
            call_params=self.infra.call.call_params,
        )

        self.context_result = await self.context_builder.build_context(self.infra)
        if self.up_to_stage == CallStage.BUILD_CONTEXT:
            await mark_call_completed(
                self.infra.call,
                self.infra.db,
                "Stopped after build_context",
            )
            return

        self.creation_result = await self.page_creator.create_pages(
            self.infra,
            self.context_result,
        )
        if self.up_to_stage == CallStage.CREATE_PAGES:
            await mark_call_completed(
                self.infra.call,
                self.infra.db,
                "Stopped after create_pages",
            )
            return

        await self.closing_reviewer.closing_review(
            self.infra,
            self.context_result,
            self.creation_result,
        )
