"""Typed trace event models for the execution tracer."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, Field, model_validator


class PageRef(BaseModel):
    id: str
    headline: str = ""

    @model_validator(mode="before")
    @classmethod
    def _migrate_summary(cls, values: dict) -> dict:
        if isinstance(values, dict) and "summary" in values and "headline" not in values:
            values["headline"] = values.pop("summary")
        return values


def _coerce_page_refs(v: list) -> list:
    """Accept both bare ID strings (legacy traces) and PageRef dicts."""
    return [{"id": x} if isinstance(x, str) else x for x in v]


PageRefList = Annotated[list[PageRef], BeforeValidator(_coerce_page_refs)]


class MoveTraceItem(BaseModel):
    type: str
    headline: str = ""
    page_refs: list[PageRef] = []
    model_config = {"extra": "allow"}


class DispatchTraceItem(BaseModel):
    call_type: str
    model_config = {"extra": "allow"}


class ContextBuiltEvent(BaseModel):
    event: Literal["context_built"] = "context_built"
    working_context_page_ids: PageRefList = []
    preloaded_page_ids: PageRefList = []
    source_page_id: str | None = None
    budget: int | None = None
    # Tiered breakdown of which pages went into the prompt at which fidelity.
    # Empty lists when the context builder doesn't run tiered selection —
    # the legacy working_context_page_ids field still carries the flat list.
    full_pages: PageRefList = []
    abstract_pages: PageRefList = []
    summary_pages: PageRefList = []
    distillation_pages: PageRefList = []
    # Pages pulled in via the scope question's linked items — considerations,
    # judgements, and sub-question judgements rendered inline by
    # format_page(scope_page, linked_detail=...). These aren't in the
    # embedding tiers; they're a separate category captured via page-load
    # tracking during build_context.
    scope_linked_pages: PageRefList = []
    # Characters spent per tier (keys: full, abstract, summary, distillation).
    budget_usage: dict[str, int] = {}
    # The rendered context section the builder produced, plus its char count
    # pre-computed so the UI can show a length at a glance without measuring.
    context_text: str = ""
    context_text_chars: int = 0
    # Per-page impact percentile assigned by the impact filter's sonnet
    # scoring pass: page_id → 1-100 (100 = above the most impactful page in
    # the standard context). Populated only by ImpactFilteredContext, and
    # only for the BFS-discovered candidate pages it scored — the inner
    # builder's baseline pages aren't scored against themselves and are
    # absent. Other context builders leave this None.
    impact_percentiles: dict[str, int] | None = None


class MovesExecutedEvent(BaseModel):
    event: Literal["moves_executed"] = "moves_executed"
    moves: list[MoveTraceItem] = []


class ReviewCompleteEvent(BaseModel):
    event: Literal["review_complete"] = "review_complete"
    remaining_fruit: float | None = None
    confidence: float | None = None


class LLMExchangeEvent(BaseModel):
    event: Literal["llm_exchange"] = "llm_exchange"
    exchange_id: str
    phase: str
    round: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    duration_ms: int | None = None
    cost_usd: float | None = None
    has_thinking: bool | None = None
    tool_uses: list[dict[str, Any]] | None = None
    langfuse_trace_url: str | None = None
    error: str | None = None


class WarningEvent(BaseModel):
    event: Literal["warning"] = "warning"
    message: str


class ErrorEvent(BaseModel):
    event: Literal["error"] = "error"
    message: str
    phase: str = ""


class RecurseFailedEvent(BaseModel):
    """Recorded on the parent prioritization trace when a planned recursive
    child cycle (recurse_into_subquestion / recurse_into_claim_investigation)
    raised before completing. Reports the failed child's call id, scope page
    id, allocated budget, and how much of the allocation was refunded back to
    the parent pool — making the silent-failure mode visible to the next
    prioritization round.
    """

    event: Literal["recurse_failed"] = "recurse_failed"
    child_call_id: str
    child_question_id: str
    child_question_headline: str = ""
    allocated_budget: int
    refunded_budget: int
    error_type: str = ""
    error_message: str = ""


class SubquestionScoreItem(BaseModel):
    question_id: str
    headline: str = ""
    impact: int | None = None
    impact_on_question: int = 0
    broader_impact: int = 0
    fruit: int = 0
    reasoning: str = ""


class CallTypeFruitScoreItem(BaseModel):
    call_type: str
    fruit: int = 0
    reasoning: str = ""


class ClaimScoreItem(BaseModel):
    page_id: str
    headline: str = ""
    impact: int | None = None
    impact_on_question: int = 0
    broader_impact: int = 0
    fruit: int = 0
    reasoning: str = ""


class ScoringCompletedEvent(BaseModel):
    event: Literal["scoring_completed"] = "scoring_completed"
    subquestion_scores: list[SubquestionScoreItem] = []
    claim_scores: list[ClaimScoreItem] = []
    # Deprecated: kept for backward compat with old traces.
    parent_fruit: int | None = None
    parent_fruit_reasoning: str = ""
    per_type_fruit: list[CallTypeFruitScoreItem] = []
    dispatch_guidance: str = ""


class ExperimentalSubquestionScoreItem(BaseModel):
    question_id: str
    headline: str = ""
    impact_curve: str = ""


class ExperimentalScoringCompletedEvent(BaseModel):
    event: Literal["experimental_scoring_completed"] = "experimental_scoring_completed"
    subquestion_scores: list[ExperimentalSubquestionScoreItem] = []
    per_type_fruit: list[CallTypeFruitScoreItem] = []


class DispatchesPlannedEvent(BaseModel):
    event: Literal["dispatches_planned"] = "dispatches_planned"
    dispatches: list[DispatchTraceItem] = []


class DispatchExecutedEvent(BaseModel):
    event: Literal["dispatch_executed"] = "dispatch_executed"
    index: int
    child_call_type: str
    question_id: str
    question_headline: str = ""
    child_call_id: str | None = None


class ExplorePageEvent(BaseModel):
    event: Literal["explore_page"] = "explore_page"
    page_id: str
    page_headline: str = ""
    response: str = ""


class SubagentStartedEvent(BaseModel):
    event: Literal["subagent_started"] = "subagent_started"
    agent_id: str
    agent_type: str
    child_call_id: str
    prompt: str = ""


class SubagentCompletedEvent(BaseModel):
    event: Literal["subagent_completed"] = "subagent_completed"
    agent_id: str
    child_call_id: str
    summary: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cost_usd: float | None = None


class AgentStartedEvent(BaseModel):
    event: Literal["agent_started"] = "agent_started"
    system_prompt: str = ""
    user_message: str = ""


class EvaluationCompleteEvent(BaseModel):
    event: Literal["evaluation_complete"] = "evaluation_complete"
    evaluation: str = ""


class ToolCallEvent(BaseModel):
    event: Literal["tool_call"] = "tool_call"
    tool_name: str
    tool_input: dict = {}
    response: str = ""


class ReassessTriggeredEvent(BaseModel):
    event: Literal["reassess_triggered"] = "reassess_triggered"
    question_id: str
    question_headline: str = ""
    child_call_id: str | None = None


class AffectedPagesIdentifiedEvent(BaseModel):
    event: Literal["affected_pages_identified"] = "affected_pages_identified"
    affected_pages: list[dict] = []


class UpdateSubgraphComputedEvent(BaseModel):
    event: Literal["update_subgraph_computed"] = "update_subgraph_computed"
    node_count: int = 0
    nodes: list[dict] = []


class UpdatePlanCreatedEvent(BaseModel):
    event: Literal["update_plan_created"] = "update_plan_created"
    wave_count: int = 0
    operation_count: int = 0
    waves: list[list[dict]] = []


class ClaimReassessedEvent(BaseModel):
    event: Literal["claim_reassessed"] = "claim_reassessed"
    old_page_id: str
    new_page_id: str
    headline: str = ""


class GroundingTasksGeneratedEvent(BaseModel):
    event: Literal["grounding_tasks_generated"] = "grounding_tasks_generated"
    task_count: int = 0
    tasks: list[dict] = []


class WebResearchCompleteEvent(BaseModel):
    event: Literal["web_research_complete"] = "web_research_complete"
    task_count: int = 0
    findings: list[dict] = []


class RenderQuestionSubgraphEvent(BaseModel):
    event: Literal["render_question_subgraph"] = "render_question_subgraph"
    page_id: str
    page_headline: str = ""
    response: str = ""


class LoadPageEvent(BaseModel):
    event: Literal["load_page"] = "load_page"
    page_id: str
    page_headline: str = ""
    detail: str = ""
    response: str = ""


class ProposedSubquestion(BaseModel):
    id: str
    headline: str = ""


class LinkSubquestionsCompleteEvent(BaseModel):
    event: Literal["link_subquestions_complete"] = "link_subquestions_complete"
    proposed: list[ProposedSubquestion] = []


class ViewCreatedEvent(BaseModel):
    event: Literal["view_created"] = "view_created"
    view_id: str
    view_headline: str = ""
    question_id: str = ""
    superseded_view_id: str | None = None


class AutocompactEvent(BaseModel):
    event: Literal["autocompact"] = "autocompact"
    agent_id: str


class PhaseSkippedEvent(BaseModel):
    event: Literal["phase_skipped"] = "phase_skipped"
    phase: str = ""
    reason: str = ""


class GlobalPhaseCompletedEvent(BaseModel):
    event: Literal["global_phase_completed"] = "global_phase_completed"
    phase: str = ""
    outcome: str = ""


class UpdateViewPhaseCompletedEvent(BaseModel):
    event: Literal["update_view_phase_completed"] = "update_view_phase_completed"
    phase: str = ""
    items_processed: int = 0
    items_modified: int = 0
    items_created: int = 0
    items_removed: int = 0


class DedupeCandidateItem(BaseModel):
    id: str
    headline: str = ""
    similarity: float
    kept_by_filter: bool = False


class QuestionDedupeEvent(BaseModel):
    event: Literal["question_dedupe"] = "question_dedupe"
    proposed_headline: str = ""
    parent_id: str
    parent_headline: str = ""
    candidates: list[DedupeCandidateItem] = []
    outcome: str = ""
    matched_page_id: str | None = None
    matched_headline: str = ""
    decision_reasoning: str = ""


class ImpactFilterEvent(BaseModel):
    event: Literal["impact_filter"] = "impact_filter"
    inner_context_chars: int = 0
    paring_triggered: bool = False
    paring_kept_pages: int | None = None
    paring_kept_chars: int | None = None
    candidates_scored: int = 0
    candidates_accepted: int = 0
    accepted_chars: int = 0
    final_threshold_percentile: int = 0
    scoring_model: str = ""
    pare_model: str | None = None


class RoundStartedEvent(BaseModel):
    """Emitted by DraftAndEditWorkflow at the top of each loop iteration.
    Mirrors two_phase's per-phase event pattern — gives the UI a
    boundary marker so it can group subsequent phase events under the
    round before any LLM call returns."""

    event: Literal["round_started"] = "round_started"
    round: int = 0


class DraftStartedEvent(BaseModel):
    """Emitted by DraftAndEditWorkflow right before the drafter LLM call
    kicks off. Lets the UI show "Drafting…" immediately instead of
    waiting on the (potentially multi-minute) call to return."""

    event: Literal["draft_started"] = "draft_started"
    round: int = 0
    model: str = ""


class CritiqueStartedEvent(BaseModel):
    """Emitted per critic, right before each critic LLM call kicks off.
    Critics run in parallel so this fires N times back-to-back at the
    start of a critique round."""

    event: Literal["critique_started"] = "critique_started"
    round: int = 0
    critic_index: int = 0
    model: str = ""


class ReadStartedEvent(BaseModel):
    """Emitted by ReflectiveJudgeWorkflow right before the read-stage
    LLM call. Live-trace counterpart to the read llm_exchange that
    fires on completion."""

    event: Literal["read_started"] = "read_started"
    model: str = ""


class ReflectStartedEvent(BaseModel):
    """Emitted by ReflectiveJudgeWorkflow right before the reflect-stage
    LLM call. Carries the prior read length so the UI has something
    concrete to show during the wait."""

    event: Literal["reflect_started"] = "reflect_started"
    model: str = ""
    prior_read_chars: int = 0


class VerdictStartedEvent(BaseModel):
    """Emitted by ReflectiveJudgeWorkflow right before the verdict-stage
    LLM call. Carries the prior read + reflect lengths so the UI
    surfaces what the verdict stage is synthesizing."""

    event: Literal["verdict_started"] = "verdict_started"
    model: str = ""
    prior_read_chars: int = 0
    prior_reflect_chars: int = 0


class EditStartedEvent(BaseModel):
    """Emitted by DraftAndEditWorkflow right before the editor LLM call
    kicks off. Carries the inputs the editor will see (current draft
    length, critique count) so the UI can show meaningful progress."""

    event: Literal["edit_started"] = "edit_started"
    round: int = 0
    model: str = ""
    current_chars: int = 0
    n_critiques: int = 0


class DraftEvent(BaseModel):
    """Emitted by DraftAndEditWorkflow when the drafter produces (or
    re-produces) a continuation. Round 0 is the initial draft; later
    rounds are post-edit drafts that the editor handed off."""

    event: Literal["draft"] = "draft"
    round: int = 0
    draft_text: str = ""
    draft_chars: int = 0
    model: str = ""


class CritiqueItem(BaseModel):
    """One critic's free-form prose output for a given round."""

    critic_index: int = 0
    critique_text: str = ""
    model: str = ""


class CritiqueRoundEvent(BaseModel):
    """Emitted by DraftAndEditWorkflow after a parallel critic fan-out
    completes for one round. ``critiques`` carries one entry per critic
    in spawn order."""

    event: Literal["critique_round"] = "critique_round"
    round: int = 0
    critiques: list[CritiqueItem] = []


class EditEvent(BaseModel):
    """Emitted by DraftAndEditWorkflow when the editor folds critiques
    into a revised draft. The resulting draft becomes the next round's
    starting point (or the final artifact if budget runs out)."""

    event: Literal["edit"] = "edit"
    round: int = 0
    revised_text: str = ""
    revised_chars: int = 0
    model: str = ""


class PlannerStartedEvent(BaseModel):
    """Emitted by DraftAndEditWorkflow right before the planner LLM call
    fires (only when with_planner=True). The planner runs once per
    workflow run, before round 0, and emits a structural brief that
    threads through every subsequent stage's user message."""

    event: Literal["planner_started"] = "planner_started"
    model: str = ""


class PlannerEvent(BaseModel):
    """Emitted by DraftAndEditWorkflow when the planner returns a brief.
    The brief is free-form structured text (xml-tagged in the default
    prompt) that the drafter / critic / editor consume verbatim. Keeping
    it as text rather than parsed JSON avoids parser-failure regressions
    if the planner deviates from format; downstream stages treat it as
    opaque context."""

    event: Literal["planner"] = "planner"
    brief_text: str = ""
    brief_chars: int = 0
    model: str = ""


class ArbitrationStartedEvent(BaseModel):
    """Emitted by DraftAndEditWorkflow right before the arbiter LLM call
    fires (only when with_arbiter=True). The arbiter runs per round
    between critique and edit; its output replaces the raw critique
    block in the editor's user message."""

    event: Literal["arbitration_started"] = "arbitration_started"
    round: int = 0
    model: str = ""
    n_critiques: int = 0


class ArbitrationEvent(BaseModel):
    """Emitted when the arbiter triages critic notes into accept / reject
    / unresolved. Like the planner brief, the arbitration text is
    free-form structured text (xml-tagged in the default prompt) that
    the editor consumes verbatim. ``prior_arbitrations_seen`` records
    how many prior rounds' arbitrations were threaded into this call so
    rejected items stay rejected across rounds without the editor
    rediscovering them."""

    event: Literal["arbitration"] = "arbitration"
    round: int = 0
    arbitration_text: str = ""
    arbitration_chars: int = 0
    prior_arbitrations_seen: int = 0
    model: str = ""


class BriefAuditStartedEvent(BaseModel):
    """Emitted by DraftAndEditWorkflow right before the brief-audit LLM
    call fires (only when with_brief_audit=True). The audit runs once
    after a designated round (default round 1's edit) and emits an
    audit brief describing what the draft has actually become — its
    real spine, real anchors, voice drift vs the original brief.
    Downstream rounds' critic + arbiter + editor see both the original
    brief and the audit side-by-side."""

    event: Literal["brief_audit_started"] = "brief_audit_started"
    after_round: int = 0
    model: str = ""


class BriefAuditEvent(BaseModel):
    """Emitted when the brief-audit returns. The audit brief is opaque
    text (same schema as the planner brief) that downstream stages
    consume verbatim alongside the original brief — they don't parse
    its internals. Recording the text in the trace event lets the UI
    show the drift between original brief and what the draft became."""

    event: Literal["brief_audit"] = "brief_audit"
    after_round: int = 0
    audit_brief_text: str = ""
    audit_brief_chars: int = 0
    model: str = ""


class ScoutPassStartedEvent(BaseModel):
    """Emitted right before the scout-pass LLM call fires (only when
    with_scout_pass=True). The scout pass runs once before the planner,
    surfacing paradigm cases / hypotheses the planner can commit to as
    mandatory anchors. Tests the v5_hybrid hypothesis: anchor-density
    is the bottleneck for v4b on philosophical / less-anchor-rich
    essays where it loses to the research-flow architecture."""

    event: Literal["scout_pass_started"] = "scout_pass_started"
    model: str = ""


class ScoutPassEvent(BaseModel):
    """Emitted when the scout pass returns. The scout findings are
    opaque text (paradigm-cases + hypotheses sections in the default
    prompt) injected into the planner's user message. Recording the
    text lets the UI show what anchors were on the table for the
    planner to choose from."""

    event: Literal["scout_pass"] = "scout_pass"
    findings_text: str = ""
    findings_chars: int = 0
    model: str = ""


TraceEvent = Annotated[
    ContextBuiltEvent
    | MovesExecutedEvent
    | ReviewCompleteEvent
    | LLMExchangeEvent
    | WarningEvent
    | ErrorEvent
    | RecurseFailedEvent
    | ScoringCompletedEvent
    | ExperimentalScoringCompletedEvent
    | DispatchesPlannedEvent
    | DispatchExecutedEvent
    | ExplorePageEvent
    | SubagentStartedEvent
    | SubagentCompletedEvent
    | AgentStartedEvent
    | EvaluationCompleteEvent
    | ToolCallEvent
    | ReassessTriggeredEvent
    | AffectedPagesIdentifiedEvent
    | UpdateSubgraphComputedEvent
    | UpdatePlanCreatedEvent
    | ClaimReassessedEvent
    | GroundingTasksGeneratedEvent
    | WebResearchCompleteEvent
    | RenderQuestionSubgraphEvent
    | LoadPageEvent
    | LinkSubquestionsCompleteEvent
    | ViewCreatedEvent
    | AutocompactEvent
    | PhaseSkippedEvent
    | GlobalPhaseCompletedEvent
    | UpdateViewPhaseCompletedEvent
    | QuestionDedupeEvent
    | ImpactFilterEvent
    | RoundStartedEvent
    | DraftStartedEvent
    | CritiqueStartedEvent
    | EditStartedEvent
    | ReadStartedEvent
    | ReflectStartedEvent
    | VerdictStartedEvent
    | DraftEvent
    | CritiqueRoundEvent
    | EditEvent
    | PlannerStartedEvent
    | PlannerEvent
    | ArbitrationStartedEvent
    | ArbitrationEvent
    | BriefAuditStartedEvent
    | BriefAuditEvent
    | ScoutPassStartedEvent
    | ScoutPassEvent,
    Field(discriminator="event"),
]
