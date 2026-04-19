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

from rumil.calls.closing_reviewers import (
    IngestClosingReview,
    SinglePhaseScoutReview,
    StandardClosingReview,
    WebResearchClosingReview,
)
from rumil.calls.context_builders import (
    BigAssessContext,
    EmbeddingContext,
    IngestEmbeddingContext,
    WebResearchEmbeddingContext,
)
from rumil.calls.page_creators import MultiRoundLoop, SimpleAgentLoop, WebResearchLoop
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
from rumil.models import CallType, FindConsiderationsMode, PageType


@register_context_builder("embedding")
def _embedding_context(ctx: StageBuildCtx, cfg: dict) -> EmbeddingContext:
    """Embedding-based context builder; uses ctx.call_type verbatim.

    Config options:
    - ``require_judgement_for_questions: bool`` (default False) — used by
      ``assess`` to require an existing judgement before considering a
      subquestion "covered".
    """
    return EmbeddingContext(
        ctx.call_type,
        require_judgement_for_questions=bool(cfg.get("require_judgement_for_questions", False)),
    )


@register_context_builder("big_assess")
def _big_assess_context(ctx: StageBuildCtx, cfg: dict) -> BigAssessContext:
    """BigAssessContext — freshens connected pages before assembling context."""
    return BigAssessContext(ctx.call_type)


@register_context_builder("web_research_embedding")
def _web_research_embedding_context(ctx: StageBuildCtx, cfg: dict) -> WebResearchEmbeddingContext:
    """WebResearchEmbeddingContext — no args, used by web_research."""
    return WebResearchEmbeddingContext()


@register_context_builder("ingest_embedding")
def _ingest_embedding_context(ctx: StageBuildCtx, cfg: dict) -> IngestEmbeddingContext:
    """IngestEmbeddingContext — requires the source Page from ctx."""
    if ctx.source_page is None:
        raise ValueError(
            "ingest_embedding context requires ctx.source_page; caller must pass "
            "stage_ctx_extras={'source_page': <Page>} to SpecCallRunner"
        )
    return IngestEmbeddingContext(ctx.source_page)


@register_workspace_updater("simple_agent_loop")
def _simple_agent_loop(ctx: StageBuildCtx, cfg: dict) -> SimpleAgentLoop:
    """Single-pass agent loop; pulls optional ``prompt_name`` from config."""
    kwargs: dict = {"available_moves": list(ctx.available_moves)}
    if cfg.get("prompt_name"):
        kwargs["prompt_name"] = cfg["prompt_name"]
    return SimpleAgentLoop(ctx.call_type, ctx.task_description, **kwargs)


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


@register_workspace_updater("web_research_loop")
def _web_research_loop(ctx: StageBuildCtx, cfg: dict) -> WebResearchLoop:
    """WebResearchLoop — server-tool-driven web search + claim creation.

    ``allowed_domains`` is pulled from stage ctx extras (populated by the
    SpecCallRunner caller when a dispatch scopes web search to a domain
    list). When absent, the loop runs unrestricted — matching the
    imperative ``WebResearchCall.__init__(... allowed_domains=None)``
    default.
    """
    allowed_domains = None
    if ctx.extras is not None:
        allowed_domains = ctx.extras.get("allowed_domains")
    return WebResearchLoop(
        allowed_domains=allowed_domains,
        available_moves=list(ctx.available_moves),
    )


@register_workspace_updater("multi_round_loop_with_mode")
def _multi_round_loop_with_mode(ctx: StageBuildCtx, cfg: dict) -> MultiRoundLoop:
    """Multi-round agent loop with a FindConsiderationsMode — matches
    ``find_considerations``'s bespoke instantiation that defers task
    wording to MultiRoundLoop's internal mode-dependent template.

    Does NOT pass ``task_description`` so MultiRoundLoop builds one
    itself from ``_resolve_round_mode`` + the mode-specific instruction
    blocks (see page_creators.py). Passing the spec's description here
    would override that behaviour.
    """
    mode_raw = cfg.get("mode", FindConsiderationsMode.ALTERNATE)
    mode = (
        mode_raw
        if isinstance(mode_raw, FindConsiderationsMode)
        else FindConsiderationsMode(mode_raw)
    )
    return MultiRoundLoop(
        int(cfg.get("max_rounds", 5)),
        int(cfg.get("fruit_threshold", 4)),
        mode,
        available_moves=list(ctx.available_moves),
    )


@register_closing_reviewer("standard_review")
def _standard_review(ctx: StageBuildCtx, cfg: dict) -> StandardClosingReview:
    """Standard closing review; uses ctx.call_type verbatim."""
    return StandardClosingReview(ctx.call_type)


@register_closing_reviewer("single_phase_scout_review")
def _single_phase_scout_review(ctx: StageBuildCtx, cfg: dict) -> SinglePhaseScoutReview:
    """SinglePhaseScoutReview — no args, used by find_considerations."""
    return SinglePhaseScoutReview()


@register_closing_reviewer("ingest_review")
def _ingest_review(ctx: StageBuildCtx, cfg: dict) -> IngestClosingReview:
    """IngestClosingReview — takes (call_type, filename). Filename comes
    from ``ctx.source_page.extra["filename"]``, falling back to the
    8-char source page id prefix — matches the imperative
    ``IngestCall.__init__`` computation.
    """
    if ctx.source_page is None:
        raise ValueError(
            "ingest_review requires ctx.source_page; caller must pass "
            "stage_ctx_extras={'source_page': <Page>} to SpecCallRunner"
        )
    extra = getattr(ctx.source_page, "extra", None) or {}
    filename = extra.get("filename") or ctx.source_page.id[:8]
    return IngestClosingReview(ctx.call_type, filename)


@register_closing_reviewer("web_research_review")
def _web_research_review(ctx: StageBuildCtx, cfg: dict) -> WebResearchClosingReview:
    """WebResearchClosingReview — threads the in-progress WebResearchLoop
    in as ``page_creator`` so the completion summary reports the real
    source count.

    ``SpecCallRunner._make_closing_reviewer`` puts the already-built
    workspace_updater under ``ctx.extras["workspace_updater"]`` (the
    closing reviewer is built last in the stage order, so this always
    resolves when this factory runs).
    """
    updater = None
    if ctx.extras is not None:
        updater = ctx.extras.get("workspace_updater")
    return WebResearchClosingReview(ctx.call_type, page_creator=updater)


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
    CallSpec(
        call_type=CallType.FIND_CONSIDERATIONS,
        description=(
            "Scout for missing considerations on a question — a parameterized "
            "multi-round loop that alternates between concrete and abstract "
            "modes by default."
        ),
        task_template=(
            "Scout for missing considerations on this question.\n\n"
            "Question ID (use this when linking considerations): "
            "`{scope_id}`"
        ),
        prompt_id="find_considerations",
        context_builder=StageRef(id="embedding"),
        workspace_updater=StageRef(
            id="multi_round_loop_with_mode",
            config={
                "max_rounds": FromCallParam("max_rounds", default=5),
                "fruit_threshold": FromCallParam("fruit_threshold", default=4),
                "mode": FromCallParam("mode", default=FindConsiderationsMode.ALTERNATE),
            },
        ),
        closing_reviewer=StageRef(id="single_phase_scout_review"),
        allowed_moves=PresetKey(""),
        scope_page_type=PageType.QUESTION,
        emits_page_types=frozenset({PageType.CLAIM, PageType.QUESTION}),
        estimated_budget_cost=5,
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


register_spec(
    CallSpec(
        call_type=CallType.ASSESS,
        description=(
            "Assess a question: synthesise considerations, weigh evidence "
            "on multiple sides, and commit to a judgement with structured "
            "confidence."
        ),
        task_template=(
            "Assess this question and render a judgement.\n\n"
            "Question ID: `{scope_id}`\n\n"
            "Synthesise the considerations, weigh evidence on multiple sides, "
            "and produce a judgement with structured confidence. "
            "Even if uncertain, commit to a position."
        ),
        prompt_id="assess",
        context_builder=StageRef(
            id="embedding",
            config={"require_judgement_for_questions": True},
        ),
        workspace_updater=StageRef(id="simple_agent_loop"),
        closing_reviewer=StageRef(id="standard_review"),
        allowed_moves=PresetKey(""),
        scope_page_type=PageType.QUESTION,
        emits_page_types=frozenset({PageType.JUDGEMENT, PageType.CLAIM}),
        estimated_budget_cost=2,
    )
)


register_spec(
    CallSpec(
        call_type=CallType.ASSESS,
        variant="big",
        description=(
            "Big-assess variant: freshens connected pages (resolves "
            "supersessions, reassesses stale deps, seeks higher-quality "
            "replacements via embedding search) before producing the "
            "judgement."
        ),
        task_template=(
            "Assess this question and render a judgement.\n\n"
            "Question ID: `{scope_id}`\n\n"
            "Follow the instructions in the system prompt."
        ),
        prompt_id="big_assess",
        context_builder=StageRef(id="big_assess"),
        workspace_updater=StageRef(
            id="simple_agent_loop",
            config={"prompt_name": "big_assess"},
        ),
        closing_reviewer=StageRef(id="standard_review"),
        allowed_moves=PresetKey(""),
        scope_page_type=PageType.QUESTION,
        emits_page_types=frozenset({PageType.JUDGEMENT, PageType.CLAIM}),
        estimated_budget_cost=3,
    )
)


register_spec(
    CallSpec(
        call_type=CallType.WEB_RESEARCH,
        description=(
            "Search the web for evidence bearing on a question and create "
            "source-grounded claims. Uses Anthropic's built-in web_search "
            "server tool plus the standard claim/consideration moves."
        ),
        task_template=(
            "Search the web for evidence relevant to this question and create "
            "source-grounded claims.\n\n"
            "Question ID (use this when linking considerations): "
            "`{scope_id}`"
        ),
        prompt_id="web_research",
        context_builder=StageRef(id="web_research_embedding"),
        workspace_updater=StageRef(id="web_research_loop"),
        closing_reviewer=StageRef(id="web_research_review"),
        allowed_moves=PresetKey(""),
        scope_page_type=PageType.QUESTION,
        emits_page_types=frozenset({PageType.CLAIM, PageType.SOURCE}),
        estimated_budget_cost=5,
    )
)


register_spec(
    CallSpec(
        call_type=CallType.INGEST,
        description=(
            "Extract considerations from a source document for a question — "
            "parameterized by the source page (passed via stage_ctx_extras) "
            "and the ``settings.ingest_num_claims`` target."
        ),
        task_template=(
            "Extract approximately {settings.ingest_num_claims} considerations "
            "from the source document above for this question. Quality over "
            "quantity — produce fewer if only fewer genuinely matter.\n\n"
            "Question ID: `{scope_id}`\n"
            "Source page ID: `{source_page_id}`"
        ),
        prompt_id="ingest",
        context_builder=StageRef(id="ingest_embedding"),
        workspace_updater=StageRef(id="simple_agent_loop"),
        closing_reviewer=StageRef(id="ingest_review"),
        allowed_moves=PresetKey(""),
        scope_page_type=PageType.QUESTION,
        emits_page_types=frozenset({PageType.CLAIM}),
        estimated_budget_cost=2,
    )
)


# ---------------------------------------------------------------------------
# Still deferred: call types that require a SpecCallRunner extension beyond
# the current StageRef + task_template + stage_ctx_extras contract.
#
# - ``CallType.CREATE_VIEW`` (``CreateViewCall``): overrides
#   ``_run_stages`` to create the View page (and superseded-view link)
#   before any stage runs, then rebuilds workspace_updater/closing_reviewer
#   with the new ``view_id``. Its ``task_description`` also interpolates
#   live ``settings.view_importance_*_cap`` values. Needs a pre-stage hook
#   for the runner-level View page setup.
# ---------------------------------------------------------------------------
