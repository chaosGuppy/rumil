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

register_spec(
    _boring_scout_spec(
        call_type=CallType.SCOUT_PARADIGM_CASES,
        prompt_id="scout_paradigm_cases",
        description=(
            "Identify paradigm cases — concrete, real-world examples that "
            "illuminate the parent question. For each case, create claims "
            "describing it and its relevance, and generate subquestions "
            "asking about its details and implications."
        ),
    )
)

register_spec(
    _boring_scout_spec(
        call_type=CallType.SCOUT_FACTCHECKS,
        prompt_id="scout_factchecks",
        description=(
            "Identify factual claims, figures, or examples in the workspace "
            "that would benefit from web-based verification. For each, create "
            "a question that a web researcher could answer — either verifying "
            "a specific assertion, finding the actual value of a quantity, or "
            "searching for known examples of a type."
        ),
    )
)

register_spec(
    _boring_scout_spec(
        call_type=CallType.SCOUT_ESTIMATES,
        prompt_id="scout_estimates",
        description=(
            "Identify quantities whose estimates would be highly informative "
            "about the parent question. Make initial guesses about their "
            "values as claims, and generate subquestions asking about those "
            "values so estimates can be refined."
        ),
    )
)

register_spec(
    _boring_scout_spec(
        call_type=CallType.SCOUT_SUBQUESTIONS,
        prompt_id="scout_subquestions",
        description=(
            "Identify subquestions whose answers would be highly informative "
            "about the parent question, and generate initial considerations "
            "that bear on the question."
        ),
    )
)

register_spec(
    _boring_scout_spec(
        call_type=CallType.SCOUT_WEB_QUESTIONS,
        prompt_id="scout_web_questions",
        description=(
            "Identify concrete factual questions whose answers would bear "
            "on the scope question and that can be answered by reading the "
            "web, without judgement or tricky reasoning. Focus on questions "
            "where you do not already confidently know the answer. For each, "
            "create a question using `create_question` (it is automatically "
            "linked as a child of the scope question). Also produce "
            "confident, non-obvious factual claims that bear on the scope "
            "question."
        ),
    )
)

register_spec(
    _boring_scout_spec(
        call_type=CallType.SCOUT_C_HOW_FALSE,
        prompt_id="scout_c_how_false",
        description=(
            "Identify plausible causal stories compatible with observed "
            "evidence but in which the scope claim is false. These are "
            "concrete alternative pictures of what might actually be going "
            "on, where the same observations hold but the claim does not. "
            "Focus on stories genuinely different from ones already "
            "identified and plausible enough to be worth taking seriously."
        ),
        scope=PageType.CLAIM,
    )
)

register_spec(
    _boring_scout_spec(
        call_type=CallType.SCOUT_C_STRENGTHEN,
        prompt_id="scout_c_strengthen",
        description=(
            "The scope claim already has high credence. Try to make it more "
            "precise, specific, or stronger while maintaining that credence. "
            "Add quantitative bounds, narrow error bars, strengthen quantifiers "
            "where evidence supports it, or add specificity. Each variation "
            "should be linked back to the original claim."
        ),
        scope=PageType.CLAIM,
    )
)

register_spec(
    _boring_scout_spec(
        call_type=CallType.SCOUT_C_ROBUSTIFY,
        prompt_id="scout_c_robustify",
        description=(
            "Suggest variations of the scope claim that are more robustly "
            "true — e.g. lower bounds instead of point estimates, conditional "
            "claims, narrower scope where evidence is strongest, or weaker "
            "quantifiers. Each variation should be linked back to the original "
            "claim and still be substantive enough to be useful. Focus on "
            "variations genuinely different from ones already identified."
        ),
        scope=PageType.CLAIM,
    )
)

register_spec(
    _boring_scout_spec(
        call_type=CallType.SCOUT_C_RELEVANT_EVIDENCE,
        prompt_id="scout_c_relevant_evidence",
        description=(
            "Identify evidence worth gathering that bears on the most "
            "important cruxes of the scope claim. Frame each as a "
            'question: "What does the literature say about X?", "What is '
            'the actual rate of Y?", "Are there documented cases of Z?" '
            "Prioritize questions that would discriminate between stories "
            "over questions whose answers would merely be consistent with "
            "one."
        ),
        scope=PageType.CLAIM,
    )
)

register_spec(
    _boring_scout_spec(
        call_type=CallType.SCOUT_C_STRESS_TEST_CASES,
        prompt_id="scout_c_stress_test_cases",
        description=(
            "Identify concrete scenarios that could serve as hard tests "
            "for the scope claim, especially boundary cases where competing "
            "stories predict different outcomes. Frame each as a question: "
            '"What does [scenario] tell us about [the claim]?" For each, '
            "describe the scenario, explain why it would be a good test, "
            "and note which stories it helps discriminate between."
        ),
        scope=PageType.CLAIM,
    )
)
