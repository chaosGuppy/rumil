"""Composition-based call abstraction: data types, stage ABCs, and CallRunner."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar

from rumil.available_moves import get_moves_for_call
from rumil.calls.common import mark_call_completed
from rumil.database import DB
from rumil.models import Call, CallStage, CallStatus, CallType, Dispatch, Move, MoveType
from rumil.moves.base import MoveState
from rumil.tracing.page_load_tracking import page_track_scope
from rumil.tracing.trace_events import ErrorEvent
from rumil.tracing.tracer import CallTrace, reset_trace, set_trace

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
class UpdateResult:
    """Output of the workspace-update stage."""

    created_page_ids: list[str]
    moves: list[Move]
    all_loaded_ids: list[str]
    dispatches: list[Dispatch] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)
    last_fruit_score: int | None = None
    rounds_completed: int = 0


@dataclass(frozen=True)
class ReviewResult:
    """Output of the closing-review stage."""

    summary: str
    review_json: dict[str, Any] = field(default_factory=dict)
    messages: Sequence[str] = field(default_factory=list)


class ContextBuilder(ABC):
    @abstractmethod
    async def build_context(self, infra: CallInfra) -> ContextResult: ...


class WorkspaceUpdater(ABC):
    @abstractmethod
    async def update_workspace(
        self,
        infra: CallInfra,
        context: ContextResult,
    ) -> UpdateResult: ...


class ClosingReviewer(ABC):
    @abstractmethod
    async def closing_review(
        self,
        infra: CallInfra,
        context: ContextResult,
        creation: UpdateResult,
    ) -> ReviewResult: ...


class CallRunner(ABC):
    """Base class for all call types using composition over inheritance.

    Subclasses set class-level stage class attributes and override
    task_description(). The CallRunner orchestrates the three stages.
    """

    context_builder_cls: ClassVar[type[ContextBuilder]]
    workspace_updater_cls: ClassVar[type[WorkspaceUpdater]]
    closing_reviewer_cls: ClassVar[type[ClosingReviewer]]
    call_type: ClassVar[CallType]

    def __init__(
        self,
        question_id: str,
        call: Call,
        db: DB,
        *,
        broadcaster=None,
        up_to_stage: CallStage | None = None,
        max_rounds: int = 5,
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
        self.update_result: UpdateResult | None = None
        self.review_result: ReviewResult | None = None
        call.call_params = {
            **(call.call_params or {}),
            "max_rounds": max_rounds,
            "fruit_threshold": fruit_threshold,
        }

        self.context_builder = self._make_context_builder()
        self.workspace_updater = self._make_workspace_updater()
        self.closing_reviewer = self._make_closing_reviewer()

    def _make_context_builder(self) -> ContextBuilder:
        return self.context_builder_cls()

    def _make_workspace_updater(self) -> WorkspaceUpdater:
        return self.workspace_updater_cls()

    def _make_closing_reviewer(self) -> ClosingReviewer:
        return self.closing_reviewer_cls()

    def _resolve_available_moves(self) -> Sequence[MoveType]:
        """Return moves from the active preset."""
        return get_moves_for_call(self.call_type)

    @property
    def review(self) -> dict:
        """Access the call's review_json for backward compatibility."""
        return self.infra.call.review_json or {}

    @property
    def result(self) -> UpdateResult:
        """Access the update result for backward compatibility."""
        if self.update_result is not None:
            return self.update_result
        return UpdateResult(created_page_ids=[], moves=[], all_loaded_ids=[])

    @abstractmethod
    def task_description(self) -> str: ...

    async def run(self) -> None:
        call_db = await self.infra.db.fork()
        self.infra.db = call_db
        self.infra.state.db = call_db
        self.infra.trace.db = call_db
        trace_token = set_trace(self.infra.trace)
        try:
            await self._run_stages()
        finally:
            try:
                await call_db.close()
            finally:
                reset_trace(trace_token)

    async def _run_stages(self) -> None:
        question_short = self.infra.question_id[:8] if self.infra.question_id else ""
        with page_track_scope(
            call_type=self.infra.call.call_type.value,
            question=question_short,
        ):
            try:
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
                    await self.infra.trace.flush_page_loads()
                    return

                self.update_result = await self.workspace_updater.update_workspace(
                    self.infra,
                    self.context_result,
                )
                if self.up_to_stage == CallStage.UPDATE_WORKSPACE:
                    await mark_call_completed(
                        self.infra.call,
                        self.infra.db,
                        "Stopped after update_workspace",
                    )
                    await self.infra.trace.flush_page_loads()
                    return

                self.review_result = await self.closing_reviewer.closing_review(
                    self.infra,
                    self.context_result,
                    self.update_result,
                )
                self.infra.call.review_json = dict(self.review_result.review_json)
                await mark_call_completed(
                    self.infra.call,
                    self.infra.db,
                    self.review_result.summary,
                )
                await self.infra.trace.flush_page_loads()
            except Exception as e:
                await self.infra.trace.flush_page_loads()
                await self.infra.trace.record(
                    ErrorEvent(
                        message=f"Call failed: {type(e).__name__}: {e}",
                        phase="run",
                    )
                )
                raise
