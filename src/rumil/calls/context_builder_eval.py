"""Build-context-only call used by the context-builder evaluation workflow.

A `ContextBuilderEvalCall` runs exactly one named context builder against a
question, stops after the build_context phase (so no workspace mutations),
and emits the standard `context_built` trace event. The CLI in
``scripts/run_context_eval.py`` pairs a gold (ImpactFilteredContext) and a
candidate (configurable) call so their loaded-page sets can be diffed.
"""

from __future__ import annotations

from collections.abc import Callable

from rumil.calls.context_builders import EmbeddingContext
from rumil.calls.impact_filtered_context import ImpactFilteredContext
from rumil.calls.stages import (
    CallInfra,
    CallRunner,
    ClosingReviewer,
    ContextBuilder,
    ContextResult,
    UpdateResult,
    WorkspaceUpdater,
)
from rumil.database import DB
from rumil.models import Call, CallStage, CallType


class _NoopWorkspaceUpdater(WorkspaceUpdater):
    """Never invoked: ContextBuilderEvalCall always stops after build_context."""

    async def update_workspace(
        self,
        infra: CallInfra,
        context: ContextResult,
    ) -> UpdateResult:
        raise RuntimeError(
            "ContextBuilderEvalCall stops after build_context; update_workspace must not run."
        )


class _NoopClosingReviewer(ClosingReviewer):
    """Never invoked: ContextBuilderEvalCall always stops after build_context."""

    async def closing_review(
        self,
        infra: CallInfra,
        context: ContextResult,
        creation: UpdateResult,
    ) -> None:
        raise RuntimeError(
            "ContextBuilderEvalCall stops after build_context; closing_review must not run."
        )


def _make_embedding_context(call_type: CallType) -> ContextBuilder:
    return EmbeddingContext(call_type)


def _make_impact_filtered_context(call_type: CallType) -> ContextBuilder:
    return ImpactFilteredContext(inner_builder=EmbeddingContext(call_type))


EVAL_CONTEXT_BUILDERS: dict[str, Callable[[CallType], ContextBuilder]] = {
    "EmbeddingContext": _make_embedding_context,
    "ImpactFilteredContext": _make_impact_filtered_context,
}

GOLD_CONTEXT_BUILDER = "ImpactFilteredContext"


def make_eval_context_builder(name: str, call_type: CallType) -> ContextBuilder:
    """Resolve a named context builder, raising a clear error on miss."""
    factory = EVAL_CONTEXT_BUILDERS.get(name)
    if factory is None:
        valid = ", ".join(sorted(EVAL_CONTEXT_BUILDERS))
        raise ValueError(f"Unknown context builder {name!r}. Valid names: {valid}")
    return factory(call_type)


class ContextBuilderEvalCall(CallRunner):
    """Run a single context builder against a question and stop after build_context.

    The workspace updater and closing reviewer are no-ops because we always
    pass ``up_to_stage=CallStage.BUILD_CONTEXT``; ``CallRunner._run_stages``
    early-returns before they would be invoked.
    """

    context_builder_cls = EmbeddingContext  # type: ignore[assignment]
    workspace_updater_cls = _NoopWorkspaceUpdater
    closing_reviewer_cls = _NoopClosingReviewer
    call_type = CallType.CONTEXT_BUILDER_EVAL

    def __init__(
        self,
        question_id: str,
        call: Call,
        db: DB,
        *,
        builder: ContextBuilder,
        builder_name: str,
        broadcaster=None,
    ) -> None:
        self._builder = builder
        self._builder_name = builder_name
        super().__init__(
            question_id,
            call,
            db,
            broadcaster=broadcaster,
            up_to_stage=CallStage.BUILD_CONTEXT,
        )

    def _make_context_builder(self) -> ContextBuilder:
        return self._builder

    @property
    def builder_name(self) -> str:
        return self._builder_name

    def task_description(self) -> str:
        return ""
