"""
Pydantic schemas for the API layer.

Composite response types and trace event envelope types. Core models
(Page, PageLink, Call, Project) live in rumil.models.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from rumil.models import Page, PageLink, _all_fields_required
from rumil.tracing.trace_events import (
    AffectedPagesIdentifiedEvent,
    AgentStartedEvent,
    ClaimReassessedEvent,
    ContextBuiltEvent,
    DispatchesPlannedEvent,
    DispatchExecutedEvent,
    ErrorEvent,
    EvaluationCompleteEvent,
    ExplorePageEvent,
    GlobalPhaseCompletedEvent,
    GroundingTasksGeneratedEvent,
    LinkSubquestionsCompleteEvent,
    LLMExchangeEvent,
    LoadPageEvent,
    MovesExecutedEvent,
    PhaseSkippedEvent,
    ReassessTriggeredEvent,
    RenderQuestionSubgraphEvent,
    ReviewCompleteEvent,
    ScoringCompletedEvent,
    SubagentCompletedEvent,
    SubagentStartedEvent,
    ToolCallEvent,
    UpdatePlanCreatedEvent,
    UpdateSubgraphComputedEvent,
    UpdateViewPhaseCompletedEvent,
    ViewCreatedEvent,
    WarningEvent,
    WebResearchCompleteEvent,
)


class LinkedPageOut(BaseModel):
    page: Page
    link: PageLink


class PageDetailOut(BaseModel):
    page: Page
    links_from: list[LinkedPageOut]
    links_to: list[LinkedPageOut]


class PageCountsOut(BaseModel):
    considerations: int
    judgements: int


class ProjectSummaryOut(BaseModel):
    """Per-project summary row for the public landing page.

    Produced by the list_projects_summary RPC in one N+1-free SQL call.
    Surfaced from GET /api/projects/summary.
    """

    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    id: str
    name: str
    created_at: datetime
    hidden: bool
    question_count: int
    claim_count: int
    call_count: int
    last_activity_at: datetime


class _TraceEnvelopeMixin(BaseModel):
    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    ts: str
    call_id: str


class ContextBuiltEventOut(ContextBuiltEvent, _TraceEnvelopeMixin):
    pass


class MovesExecutedEventOut(MovesExecutedEvent, _TraceEnvelopeMixin):
    pass


class ReviewCompleteEventOut(ReviewCompleteEvent, _TraceEnvelopeMixin):
    pass


class LLMExchangeEventOut(LLMExchangeEvent, _TraceEnvelopeMixin):
    pass


class WarningEventOut(WarningEvent, _TraceEnvelopeMixin):
    pass


class ErrorEventOut(ErrorEvent, _TraceEnvelopeMixin):
    pass


class ScoringCompletedEventOut(ScoringCompletedEvent, _TraceEnvelopeMixin):
    pass


class DispatchesPlannedEventOut(DispatchesPlannedEvent, _TraceEnvelopeMixin):
    pass


class DispatchExecutedEventOut(DispatchExecutedEvent, _TraceEnvelopeMixin):
    pass


class ExplorePageEventOut(ExplorePageEvent, _TraceEnvelopeMixin):
    pass


class SubagentStartedEventOut(SubagentStartedEvent, _TraceEnvelopeMixin):
    pass


class SubagentCompletedEventOut(SubagentCompletedEvent, _TraceEnvelopeMixin):
    pass


class AgentStartedEventOut(AgentStartedEvent, _TraceEnvelopeMixin):
    pass


class EvaluationCompleteEventOut(EvaluationCompleteEvent, _TraceEnvelopeMixin):
    pass


class ToolCallEventOut(ToolCallEvent, _TraceEnvelopeMixin):
    pass


class ReassessTriggeredEventOut(ReassessTriggeredEvent, _TraceEnvelopeMixin):
    pass


class AffectedPagesIdentifiedEventOut(AffectedPagesIdentifiedEvent, _TraceEnvelopeMixin):
    pass


class UpdateSubgraphComputedEventOut(UpdateSubgraphComputedEvent, _TraceEnvelopeMixin):
    pass


class UpdatePlanCreatedEventOut(UpdatePlanCreatedEvent, _TraceEnvelopeMixin):
    pass


class ClaimReassessedEventOut(ClaimReassessedEvent, _TraceEnvelopeMixin):
    pass


class GroundingTasksGeneratedEventOut(GroundingTasksGeneratedEvent, _TraceEnvelopeMixin):
    pass


class WebResearchCompleteEventOut(WebResearchCompleteEvent, _TraceEnvelopeMixin):
    pass


class RenderQuestionSubgraphEventOut(RenderQuestionSubgraphEvent, _TraceEnvelopeMixin):
    pass


class LoadPageEventOut(LoadPageEvent, _TraceEnvelopeMixin):
    pass


class LinkSubquestionsCompleteEventOut(LinkSubquestionsCompleteEvent, _TraceEnvelopeMixin):
    pass


class ViewCreatedEventOut(ViewCreatedEvent, _TraceEnvelopeMixin):
    pass


class PhaseSkippedEventOut(PhaseSkippedEvent, _TraceEnvelopeMixin):
    pass


class GlobalPhaseCompletedEventOut(GlobalPhaseCompletedEvent, _TraceEnvelopeMixin):
    pass


class UpdateViewPhaseCompletedEventOut(UpdateViewPhaseCompletedEvent, _TraceEnvelopeMixin):
    pass


TraceEventOut = Annotated[
    ContextBuiltEventOut
    | MovesExecutedEventOut
    | ReviewCompleteEventOut
    | LLMExchangeEventOut
    | WarningEventOut
    | ErrorEventOut
    | ScoringCompletedEventOut
    | DispatchesPlannedEventOut
    | DispatchExecutedEventOut
    | ExplorePageEventOut
    | SubagentStartedEventOut
    | SubagentCompletedEventOut
    | AgentStartedEventOut
    | EvaluationCompleteEventOut
    | ToolCallEventOut
    | ReassessTriggeredEventOut
    | AffectedPagesIdentifiedEventOut
    | UpdateSubgraphComputedEventOut
    | UpdatePlanCreatedEventOut
    | ClaimReassessedEventOut
    | GroundingTasksGeneratedEventOut
    | WebResearchCompleteEventOut
    | RenderQuestionSubgraphEventOut
    | LoadPageEventOut
    | LinkSubquestionsCompleteEventOut
    | ViewCreatedEventOut
    | PhaseSkippedEventOut
    | GlobalPhaseCompletedEventOut
    | UpdateViewPhaseCompletedEventOut,
    Field(discriminator="event"),
]


class LLMExchangeSummaryOut(BaseModel):
    id: str
    phase: str
    round: int | None
    input_tokens: int | None
    output_tokens: int | None
    duration_ms: int | None
    error: str | None
    created_at: datetime


class LLMExchangeOut(BaseModel):
    id: str
    call_id: str
    phase: str
    round: int | None
    system_prompt: str | None
    user_message: str | None
    user_messages: list[dict] | None = None
    response_text: str | None
    tool_calls: list[dict]
    input_tokens: int | None
    output_tokens: int | None
    duration_ms: int | None
    error: str | None
    created_at: datetime


class CallSummary(BaseModel):
    """Lightweight Call representation for tree views — excludes bulky fields
    like review_json, result_summary, and context_page_ids."""

    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    id: str
    call_type: str
    status: str
    parent_call_id: str | None = None
    scope_page_id: str | None = None
    call_params: dict | None = None
    created_at: datetime
    completed_at: datetime | None = None
    sequence_id: str | None = None
    sequence_position: int | None = None
    cost_usd: float | None = None


class CallNodeOut(BaseModel):
    call: CallSummary
    scope_page_summary: str | None = None
    warning_count: int = 0
    error_count: int = 0


class RunTraceTreeOut(BaseModel):
    run_id: str
    question: Page | None
    calls: list[CallNodeOut]
    cost_usd: float | None = None
    staged: bool = False
    config: dict = {}


class RunSummaryOut(BaseModel):
    run_id: str
    created_at: str
    provenance_call_id: str = ""


class RunListItemOut(BaseModel):
    run_id: str | None = None
    created_at: str
    name: str = ""
    config: dict | None = None
    question_summary: str | None = None
    staged: bool = False


class PaginatedPagesOut(BaseModel):
    model_config = ConfigDict(json_schema_extra=_all_fields_required)
    items: Sequence[Page]
    total_count: int
    offset: int
    limit: int


class ABEvalDimensionOut(BaseModel):
    name: str
    display_name: str
    preference: str
    report_a: str
    report_b: str
    comparison: str
    call_id_a: str = ""
    call_id_b: str = ""
    comparison_call_id: str = ""


class ABEvalReportOut(BaseModel):
    id: str
    run_id_a: str
    run_id_b: str
    question_id_a: str = ""
    question_id_b: str = ""
    question_headline: str = ""
    overall_assessment: str
    overall_assessment_call_id: str = ""
    dimension_reports: list[ABEvalDimensionOut]
    config_a: dict = {}
    config_b: dict = {}
    created_at: str


class ABEvalDimensionSummaryOut(BaseModel):
    name: str
    display_name: str
    preference: str


class ABEvalReportListItemOut(BaseModel):
    id: str
    run_id_a: str
    run_id_b: str
    question_id_a: str = ""
    question_id_b: str = ""
    question_headline: str = ""
    overall_assessment_preview: str = ""
    preferences: list[ABEvalDimensionSummaryOut]
    created_at: str


class RealtimeConfigOut(BaseModel):
    url: str
    anon_key: str


class DegreeCell(BaseModel):
    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    avg_out: float
    avg_in: float


class CallsForQuestion(BaseModel):
    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    question_id: str
    headline: str | None
    by_type: dict[str, int]
    total: int


class StatsOut(BaseModel):
    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    pages_total: int
    pages_by_type: dict[str, int]
    links_total: int
    links_by_type: dict[str, int]
    degree_matrix: dict[str, dict[str, DegreeCell]]
    robustness_histogram: dict[str, int]
    credence_histogram: dict[str, int]
    calls_per_question: list[CallsForQuestion]


class ProjectStatsOut(StatsOut):
    project_id: str


class SubgraphNode(BaseModel):
    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    id: str
    page_type: str
    headline: str | None
    depth: int


class SubgraphEdge(BaseModel):
    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    from_page_id: str
    to_page_id: str
    link_type: str


class Subgraph(BaseModel):
    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    nodes: list[SubgraphNode]
    edges: list[SubgraphEdge]


class QuestionStatsOut(StatsOut):
    question_id: str
    subgraph_page_count: int
    subgraph: Subgraph


class PageLoadEventOut(BaseModel):
    page_id: str
    detail: str
    tags: dict[str, str]


class PageLoadStatsOut(BaseModel):
    events: list[PageLoadEventOut]
    total: int
    total_unique: int


class ReputationBucketOut(BaseModel):
    """One aggregated (source, dimension, orchestrator) bucket.

    Sources are intentionally not collapsed — consumers render buckets
    separately so eval_agent and human_feedback scores are never mixed
    into a single number. See marketplace-thread/13-reputation-governance.md.
    """

    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    source: str
    dimension: str
    orchestrator: str | None = None
    n_events: int
    mean_score: float
    min_score: float
    max_score: float
    latest_at: str


class ReputationSummaryOut(BaseModel):
    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    project_id: str
    total_events: int
    buckets: list[ReputationBucketOut]


class ViewItemFlagRequest(BaseModel):
    category: Annotated[
        str,
        Field(
            pattern=(
                "^("
                "problem|improvement"
                "|factually_wrong|missing_consideration"
                "|reasoning_flawed|scope_confused|other"
                ")$"
            ),
        ),
    ]
    message: str
    suggested_fix: str = ""


class ViewItemReadRequest(BaseModel):
    seconds: float = 0.0


class ViewItemReadOut(BaseModel):
    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    ok: bool
    page_id: str


class ViewItemFlagDeleteOut(BaseModel):
    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    ok: bool
    flag_id: str


class ViewItemFlagOut(BaseModel):
    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    ok: bool
    flag_id: str
    page_id: str


class AppConfigOut(BaseModel):
    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    enable_flag_issue: bool


class AnnotationCreateRequest(BaseModel):
    annotation_type: Annotated[
        str,
        Field(pattern="^(span|counterfactual_tool_use|flag|endorsement)$"),
    ]
    target_page_id: str | None = None
    target_call_id: str | None = None
    target_event_seq: int | None = None
    span_start: int | None = None
    span_end: int | None = None
    category: str | None = None
    note: str = ""
    payload: dict = Field(default_factory=dict)
    extra: dict = Field(default_factory=dict)


class AnnotationCreateOut(BaseModel):
    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    ok: bool
    annotation_id: str


class AdversarialVerdictSummaryOut(BaseModel):
    """Summary of a single adversarial-review verdict targeting a page.

    Produced by GET /api/pages/{page_id}/adversarial-verdicts. The frontend
    renders this inline as a ``VerdictBadge`` next to credence/robustness.

    Field mapping from the underlying ``AdversarialVerdict``
    (src/rumil/calls/adversarial_review.py):

    - ``stronger_side`` → which scout's case was stronger
      (``"how_true"`` / ``"how_false"`` / ``"tie"``)
    - ``claim_holds`` → whether the claim survived review
    - ``confidence``, ``rationale`` → synthesizer's epistemic read
    - ``concurrences``, ``dissents`` → notable secondary points worth
      preserving
    - ``sunset_after_days`` + ``verdict_created_at`` → shelf-life. When
      expired, the frontend mutes the badge and adds a "stale" tag.
    """

    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    verdict_page_id: str
    target_page_id: str
    stronger_side: str
    claim_holds: bool
    confidence: int
    rationale: str
    concurrences: list[str]
    dissents: list[str]
    sunset_after_days: int | None
    verdict_created_at: datetime
    expired: bool
    page_created_at: datetime
