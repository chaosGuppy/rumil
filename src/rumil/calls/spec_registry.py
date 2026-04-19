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


def _boring_scout_spec(
    *,
    call_type: CallType,
    prompt_id: str,
    description: str,
    scope: PageType = PageType.QUESTION,
    emits: frozenset[PageType] = frozenset({PageType.CLAIM, PageType.QUESTION}),
) -> CallSpec:
    """Build the CallSpec for a "boring scout" — embedding context +
    multi-round loop + standard closing review. Params are pulled from
    the call's runtime ``call_params`` so orchestrators can still
    override max_rounds / fruit_threshold per-dispatch.
    """
    return CallSpec(
        call_type=call_type,
        description=description,
        prompt_id=prompt_id,
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
        scope_page_type=scope,
        emits_page_types=emits,
        estimated_budget_cost=5,
    )


register_spec(
    _boring_scout_spec(
        call_type=CallType.SCOUT_ANALOGIES,
        prompt_id="scout_analogies",
        description=(
            "Identify analogies that may be informative about the parent "
            "question. For each analogy, create claims describing it and its "
            "relevance, and generate subquestions asking about the details "
            "and limits of the analogy."
        ),
    )
)

register_spec(
    _boring_scout_spec(
        call_type=CallType.SCOUT_DEEP_QUESTIONS,
        prompt_id="scout_deep_questions",
        description=(
            "Identify important questions bearing on the scope question that "
            "require judgement, interpretation, or involved reasoning to answer "
            "— questions that cannot be resolved by simply looking something up. "
            "For each, create a question using `create_question` (it is "
            "automatically linked as a child of the scope question). Also "
            "produce confident, non-obvious high-level claims that bear on "
            "the scope question."
        ),
    )
)

register_spec(
    _boring_scout_spec(
        call_type=CallType.SCOUT_HYPOTHESES,
        prompt_id="scout_hypotheses",
        description=(
            "Identify hypotheses that should be explored as potential answers "
            "to the parent question. For each hypothesis, create a claim "
            "stating the hypothesis and link it as a consideration to the "
            "parent question. Set credence and robustness honestly — these "
            "are initial assessments."
        ),
    )
)

register_spec(
    _boring_scout_spec(
        call_type=CallType.SCOUT_C_CRUXES,
        prompt_id="scout_c_cruxes",
        description=(
            "Identify cruxes — specific points where the how-true and "
            "how-false stories diverge, such that resolving them would "
            "tell you which story is closer to the truth. A crux may be "
            "a claim (something whose truth is load-bearing) or a question "
            "(something whose answer would discriminate between stories). "
            "Rank by importance and tractability."
        ),
        scope=PageType.CLAIM,
    )
)

register_spec(
    _boring_scout_spec(
        call_type=CallType.SCOUT_C_HOW_TRUE,
        prompt_id="scout_c_how_true",
        description=(
            "Identify plausible causal mechanisms that would make the "
            "scope claim true. For each story: what is the mechanism? "
            "What is actually going on in the world that makes the claim "
            "true? What observable consequences would we expect if this "
            "mechanism is operating? Be specific and concrete about the "
            "causal chain. Focus on stories genuinely different from ones "
            "already identified."
        ),
        scope=PageType.CLAIM,
    )
)
