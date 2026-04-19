"""Baseline stage factories + per-call-type ``CallSpec`` registrations.

Imported for its side effects (registering entries in the stage
registries + ``SPECS``). Keep import-only behavior — no logic beyond
``register_*`` decorators — so ``from rumil.calls import spec_registry``
wires the shared catalog without further orchestration.

Today only a handful of specs are registered: proof-of-shape for the
CallSpec refactor. Converting the remaining ~30 imperative call types
is tracked in the master plan.
"""

from __future__ import annotations

from rumil.calls.closing_reviewers import StandardClosingReview
from rumil.calls.context_builders import EmbeddingContext
from rumil.calls.page_creators import MultiRoundLoop
from rumil.calls.spec import (
    CallSpec,
    FromCallParam,
    PresetKey,
    StageBuildCtx,
    StageRef,
    register_closing_reviewer,
    register_context_builder,
    register_spec,
    register_workspace_updater,
)
from rumil.models import CallType, PageType


@register_context_builder("embedding")
def _embedding_context(ctx: StageBuildCtx, cfg: dict) -> EmbeddingContext:
    """Embedding-based context builder; uses ctx.call_type verbatim."""
    return EmbeddingContext(ctx.call_type)


@register_workspace_updater("multi_round_loop")
def _multi_round_loop(ctx: StageBuildCtx, cfg: dict) -> MultiRoundLoop:
    """Multi-round agent loop; pulls max_rounds/fruit_threshold from config."""
    return MultiRoundLoop(
        int(cfg.get("max_rounds", 5)),
        int(cfg.get("fruit_threshold", 4)),
        available_moves=list(ctx.available_moves),
        call_type=ctx.call_type,
        task_description=ctx.task_description,
    )


@register_closing_reviewer("standard_review")
def _standard_review(ctx: StageBuildCtx, cfg: dict) -> StandardClosingReview:
    """Standard closing review; uses ctx.call_type verbatim."""
    return StandardClosingReview(ctx.call_type)


_SCOUT_ANALOGIES_DESC = (
    "Identify analogies that may be informative about the parent "
    "question. For each analogy, create claims describing it and its "
    "relevance, and generate subquestions asking about the details "
    "and limits of the analogy."
)


register_spec(
    CallSpec(
        call_type=CallType.SCOUT_ANALOGIES,
        description=_SCOUT_ANALOGIES_DESC,
        prompt_id="scout_analogies",
        context_builder=StageRef(id="embedding"),
        workspace_updater=StageRef(
            id="multi_round_loop",
            config={
                "max_rounds": FromCallParam("max_rounds", default=5),
                "fruit_threshold": FromCallParam("fruit_threshold", default=4),
            },
        ),
        closing_reviewer=StageRef(id="standard_review"),
        allowed_moves=PresetKey(""),
        scope_page_type=PageType.QUESTION,
        emits_page_types=frozenset({PageType.CLAIM, PageType.QUESTION}),
        estimated_budget_cost=5,
    )
)
