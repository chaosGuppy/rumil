"""Scout Stress-Test Cases call: identify concrete scenarios as hard tests for a claim."""

from rumil.calls.closing_reviewers import StandardClosingReview
from rumil.calls.context_builders import EmbeddingContext
from rumil.calls.page_creators import MultiRoundLoop
from rumil.calls.stages import (
    CallRunner,
    ClosingReviewer,
    ContextBuilder,
    WorkspaceUpdater,
)
from rumil.models import CallType


class ScoutCStressTestCasesCall(CallRunner):
    """Identify concrete scenarios serving as hard tests for the claim."""

    context_builder_cls = EmbeddingContext
    workspace_updater_cls = MultiRoundLoop
    closing_reviewer_cls = StandardClosingReview
    call_type = CallType.SCOUT_C_STRESS_TEST_CASES

    def _make_context_builder(self) -> ContextBuilder:
        return EmbeddingContext(self.call_type)

    def _make_workspace_updater(self) -> WorkspaceUpdater:
        return MultiRoundLoop(
            self._max_rounds,
            self._fruit_threshold,
            available_moves=self._resolve_available_moves(),
            call_type=self.call_type,
            task_description=self.task_description(),
        )

    def _make_closing_reviewer(self) -> ClosingReviewer:
        return StandardClosingReview(self.call_type)

    def task_description(self) -> str:
        return (
            "Identify concrete scenarios that could serve as hard tests "
            "for the scope claim, especially boundary cases where competing "
            "stories predict different outcomes. Frame each as a question: "
            '"What does [scenario] tell us about [the claim]?" For each, '
            "describe the scenario, explain why it would be a good test, "
            "and note which stories it helps discriminate between.\n\n"
            f"Claim ID: `{self.infra.question_id}`"
        )
