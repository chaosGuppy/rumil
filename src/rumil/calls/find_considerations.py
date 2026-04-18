"""Find considerations call: find missing considerations on a question."""

from collections.abc import Sequence

from rumil.calls.closing_reviewers import SinglePhaseScoutReview
from rumil.calls.context_builders import EmbeddingContext
from rumil.calls.page_creators import MultiRoundLoop
from rumil.calls.stages import (
    CallRunner,
    ClosingReviewer,
    ContextBuilder,
    WorkspaceUpdater,
)
from rumil.database import DB
from rumil.models import Call, CallStage, CallType


class FindConsiderationsCall(CallRunner):
    """Multi-round scout session with fruit checking."""

    context_builder_cls = EmbeddingContext
    workspace_updater_cls = MultiRoundLoop
    closing_reviewer_cls = SinglePhaseScoutReview
    call_type = CallType.FIND_CONSIDERATIONS

    def __init__(
        self,
        question_id: str,
        call: Call,
        db: DB,
        *,
        max_rounds: int,
        fruit_threshold: int,
        context_page_ids: Sequence[str] | None = None,
        broadcaster=None,
        up_to_stage: CallStage | None = None,
    ):
        call.call_params = {
            **(call.call_params or {}),
            "max_rounds": max_rounds,
            "fruit_threshold": fruit_threshold,
        }
        self._max_rounds = max_rounds
        self._fruit_threshold = fruit_threshold
        self._context_page_ids = context_page_ids
        super().__init__(question_id, call, db, broadcaster=broadcaster, up_to_stage=up_to_stage)

    @property
    def rounds_completed(self) -> int:
        if self.update_result is not None:
            return self.update_result.rounds_completed
        return 0

    def _make_context_builder(self) -> ContextBuilder:
        return EmbeddingContext(self.call_type, require_judgement_for_questions=True)

    def _make_workspace_updater(self) -> WorkspaceUpdater:
        return MultiRoundLoop(
            self._max_rounds,
            self._fruit_threshold,
            available_moves=self._resolve_available_moves(),
        )

    def _make_closing_reviewer(self) -> ClosingReviewer:
        return SinglePhaseScoutReview()

    def task_description(self) -> str:
        return (
            "Generate considerations that would most improve the next judgement "
            "on this question.\n\n"
            "Question ID (use this when linking considerations): "
            f"`{self.infra.question_id}`"
        )
