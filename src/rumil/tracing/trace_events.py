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
    scout_mode: str | None = None
    # Per-page rendering tier from the embedding context builder:
    # "distillation" | "full" | "abstract" | "summary". Pages omitted from
    # this map (e.g. preloaded, scope, view items) have no tier classification.
    # Optional / nullable so older trace_json rows still parse.
    page_id_tiers: dict[str, str] | None = None


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
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    duration_ms: int | None = None
    cost_usd: float | None = None
    has_thinking: bool | None = None
    tool_uses: list[dict[str, Any]] | None = None
    # Stable content hash of the system prompt this exchange used, joined to
    # prompt_versions on read. Null for legacy exchanges and for subagent
    # turns that don't go through save_llm_exchange.
    composite_prompt_hash: str | None = None
    # Friendly name of the prompt (e.g. "big_assess", "composite"). Logged at
    # save time for UI display without an extra round trip.
    prompt_name: str | None = None


class WarningEvent(BaseModel):
    event: Literal["warning"] = "warning"
    message: str


class ErrorEvent(BaseModel):
    event: Literal["error"] = "error"
    message: str
    phase: str = ""


class EvalDimensionSummaryItem(BaseModel):
    """One dimension's summarized reputation-event signal for a page.

    Emitted as part of ``ScoringCompletedEvent`` so the prioritization UI
    can show the same quality signal the LLM saw in its prompt.
    """

    dimension: str
    mean: float = 0.0
    count: int = 0
    latest: float = 0.0


class SubquestionScoreItem(BaseModel):
    question_id: str
    headline: str = ""
    impact: int | None = None
    impact_on_question: int = 0
    broader_impact: int = 0
    fruit: int = 0
    reasoning: str = ""
    eval_summary: list[EvalDimensionSummaryItem] = []


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
    eval_summary: list[EvalDimensionSummaryItem] = []


class ScoringCompletedEvent(BaseModel):
    event: Literal["scoring_completed"] = "scoring_completed"
    subquestion_scores: list[SubquestionScoreItem] = []
    claim_scores: list[ClaimScoreItem] = []
    # Deprecated: kept for backward compat with old traces.
    parent_fruit: int | None = None
    parent_fruit_reasoning: str = ""
    per_type_fruit: list[CallTypeFruitScoreItem] = []
    dispatch_guidance: str = ""


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


class NudgeSummaryItem(BaseModel):
    """Summary of one nudge that fired at a safe point.

    Emitted under ``NudgeAppliedEvent`` so the trace UI can show which
    human-authored nudges influenced this orchestrator tick / call, what
    they did, and whether any hard filters removed dispatch candidates.
    """

    nudge_id: str
    kind: str
    author_kind: str = ""
    hard: bool = False
    soft_text: str | None = None
    effect: str = ""


class NudgeAppliedEvent(BaseModel):
    event: Literal["nudge_applied"] = "nudge_applied"
    phase: str = ""
    applied: list[NudgeSummaryItem] = []
    filtered_dispatch_count: int = 0


TraceEvent = Annotated[
    ContextBuiltEvent
    | MovesExecutedEvent
    | ReviewCompleteEvent
    | LLMExchangeEvent
    | WarningEvent
    | ErrorEvent
    | ScoringCompletedEvent
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
    | PhaseSkippedEvent
    | GlobalPhaseCompletedEvent
    | UpdateViewPhaseCompletedEvent
    | NudgeAppliedEvent,
    Field(discriminator="event"),
]
