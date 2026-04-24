"""Refine Spec call: iterate on the spec in response to artefact + critique.

This is the driver of the generative workflow. Reads the current spec and
the last-3 iteration triples (spec → artefact → critique), edits the spec
via add/supersede/delete moves, and fires regenerate_and_critique when
ready to see how the artefact has moved. Exits by calling
finalize_artefact when further iteration is unlikely to help, or when
the outer orchestrator exhausts its budget.
"""

from rumil.calls.closing_reviewers import StandardClosingReview
from rumil.calls.context_builders import RefinementContext
from rumil.calls.page_creators import SimpleAgentLoop
from rumil.calls.stages import (
    CallRunner,
    ClosingReviewer,
    ContextBuilder,
    WorkspaceUpdater,
)
from rumil.models import CallType


class RefineSpecCall(CallRunner):
    """Iterate the spec via an agent loop until the artefact converges or budget runs out."""

    context_builder_cls = RefinementContext
    workspace_updater_cls = SimpleAgentLoop
    closing_reviewer_cls = StandardClosingReview
    call_type = CallType.REFINE_SPEC

    def _make_context_builder(self) -> ContextBuilder:
        return RefinementContext()

    def _make_workspace_updater(self) -> WorkspaceUpdater:
        return SimpleAgentLoop(
            self.call_type,
            self.task_description(),
            available_moves=self._resolve_available_moves(),
            max_rounds=self._max_rounds,
        )

    def _make_closing_reviewer(self) -> ClosingReviewer:
        return StandardClosingReview(self.call_type)

    def task_description(self) -> str:
        return (
            "Refine the spec for this artefact-task. You may add, supersede, "
            "or delete spec items, then call `regenerate_and_critique` to see "
            "how the artefact moves. Call `finalize_artefact` when further "
            "iteration is unlikely to help — the artefact is good enough, or "
            "the request is too open-ended to converge, or the remaining "
            "issues need workspace signal the current spec can't capture.\n\n"
            f"Artefact-task question ID: `{self.infra.question_id}`"
        )
