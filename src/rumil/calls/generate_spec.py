"""Generate Spec call: produce the initial set of spec items for an artefact task."""

from rumil.calls.closing_reviewers import StandardClosingReview
from rumil.calls.context_builders import EmbeddingContext
from rumil.calls.page_creators import SimpleAgentLoop
from rumil.calls.stages import (
    CallRunner,
    ClosingReviewer,
    ContextBuilder,
    WorkspaceUpdater,
)
from rumil.database import DB
from rumil.models import Call, CallStage, CallType


class GenerateSpecCall(CallRunner):
    """Write the initial spec for an artefact the generative workflow will produce.

    The call runs against a (typically hidden) artefact-task question. It has
    full workspace context via embedding search and emits spec items via the
    ADD_SPEC_ITEM move. Each spec item is a hidden SPEC_ITEM page linked to
    the artefact-task question via SPEC_OF.

    *prompt_variant*, when set, selects an alternate prompt file
    (``generate_spec_<variant>.md``) instead of the default
    ``generate_spec.md``. Used by experimental settings such as the
    ``--spec-size {tight,loose}`` toggle on the generative workflow.
    """

    context_builder_cls = EmbeddingContext
    workspace_updater_cls = SimpleAgentLoop
    closing_reviewer_cls = StandardClosingReview
    call_type = CallType.GENERATE_SPEC

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
        pool_question_id: str | None = None,
        prompt_variant: str | None = None,
    ) -> None:
        self._prompt_variant = prompt_variant
        super().__init__(
            question_id,
            call,
            db,
            broadcaster=broadcaster,
            up_to_stage=up_to_stage,
            max_rounds=max_rounds,
            fruit_threshold=fruit_threshold,
            pool_question_id=pool_question_id,
        )

    def _make_context_builder(self) -> ContextBuilder:
        return EmbeddingContext(self.call_type)

    def _make_workspace_updater(self) -> WorkspaceUpdater:
        return SimpleAgentLoop(
            self.call_type,
            self.task_description(),
            available_moves=self._resolve_available_moves(),
            prompt_name=self._resolved_prompt_name(),
        )

    def _make_closing_reviewer(self) -> ClosingReviewer:
        return StandardClosingReview(self.call_type)

    def _resolved_prompt_name(self) -> str | None:
        if self._prompt_variant:
            return f"{self.call_type.value}_{self._prompt_variant}"
        return None

    def task_description(self) -> str:
        return (
            "Write the initial spec for the artefact described by the "
            "artefact-task question below. Emit spec items via `add_spec_item` — "
            "each one is a single atomic prescriptive rule the artefact will be "
            "held to. Aim for a spec rich enough that a downstream generator, "
            "seeing only the spec, could write a faithful artefact.\n\n"
            f"Artefact-task question ID: `{self.infra.question_id}`"
        )
