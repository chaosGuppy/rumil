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
    AutocompactEvent,
    ClaimReassessedEvent,
    ContextBuiltEvent,
    CritiqueRoundEvent,
    CritiqueStartedEvent,
    DispatchesPlannedEvent,
    DispatchExecutedEvent,
    DraftEvent,
    DraftStartedEvent,
    EditEvent,
    EditStartedEvent,
    ErrorEvent,
    EvaluationCompleteEvent,
    ExperimentalScoringCompletedEvent,
    ExplorePageEvent,
    GlobalPhaseCompletedEvent,
    GroundingTasksGeneratedEvent,
    ImpactFilterEvent,
    LinkSubquestionsCompleteEvent,
    LLMExchangeEvent,
    LoadPageEvent,
    MovesExecutedEvent,
    PhaseSkippedEvent,
    QuestionDedupeEvent,
    ReadStartedEvent,
    ReassessTriggeredEvent,
    ReflectStartedEvent,
    RenderQuestionSubgraphEvent,
    ReviewCompleteEvent,
    RoundStartedEvent,
    ScoringCompletedEvent,
    SubagentCompletedEvent,
    SubagentStartedEvent,
    ToolCallEvent,
    UpdatePlanCreatedEvent,
    UpdateSubgraphComputedEvent,
    UpdateViewPhaseCompletedEvent,
    VerdictStartedEvent,
    ViewCreatedEvent,
    WarningEvent,
    WebResearchCompleteEvent,
)


class AuthUserOut(BaseModel):
    model_config = ConfigDict(json_schema_extra=_all_fields_required)
    user_id: str
    email: str
    is_admin: bool


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


class ExperimentalScoringCompletedEventOut(ExperimentalScoringCompletedEvent, _TraceEnvelopeMixin):
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


class AutocompactEventOut(AutocompactEvent, _TraceEnvelopeMixin):
    pass


class PhaseSkippedEventOut(PhaseSkippedEvent, _TraceEnvelopeMixin):
    pass


class GlobalPhaseCompletedEventOut(GlobalPhaseCompletedEvent, _TraceEnvelopeMixin):
    pass


class UpdateViewPhaseCompletedEventOut(UpdateViewPhaseCompletedEvent, _TraceEnvelopeMixin):
    pass


class QuestionDedupeEventOut(QuestionDedupeEvent, _TraceEnvelopeMixin):
    pass


class ImpactFilterEventOut(ImpactFilterEvent, _TraceEnvelopeMixin):
    pass


class RoundStartedEventOut(RoundStartedEvent, _TraceEnvelopeMixin):
    pass


class DraftStartedEventOut(DraftStartedEvent, _TraceEnvelopeMixin):
    pass


class CritiqueStartedEventOut(CritiqueStartedEvent, _TraceEnvelopeMixin):
    pass


class EditStartedEventOut(EditStartedEvent, _TraceEnvelopeMixin):
    pass


class ReadStartedEventOut(ReadStartedEvent, _TraceEnvelopeMixin):
    pass


class ReflectStartedEventOut(ReflectStartedEvent, _TraceEnvelopeMixin):
    pass


class VerdictStartedEventOut(VerdictStartedEvent, _TraceEnvelopeMixin):
    pass


class DraftEventOut(DraftEvent, _TraceEnvelopeMixin):
    pass


class CritiqueRoundEventOut(CritiqueRoundEvent, _TraceEnvelopeMixin):
    pass


class EditEventOut(EditEvent, _TraceEnvelopeMixin):
    pass


TraceEventOut = Annotated[
    ContextBuiltEventOut
    | MovesExecutedEventOut
    | ReviewCompleteEventOut
    | LLMExchangeEventOut
    | WarningEventOut
    | ErrorEventOut
    | ScoringCompletedEventOut
    | ExperimentalScoringCompletedEventOut
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
    | AutocompactEventOut
    | PhaseSkippedEventOut
    | GlobalPhaseCompletedEventOut
    | UpdateViewPhaseCompletedEventOut
    | QuestionDedupeEventOut
    | ImpactFilterEventOut
    | RoundStartedEventOut
    | DraftStartedEventOut
    | CritiqueStartedEventOut
    | EditStartedEventOut
    | ReadStartedEventOut
    | ReflectStartedEventOut
    | VerdictStartedEventOut
    | DraftEventOut
    | CritiqueRoundEventOut
    | EditEventOut,
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
    report: str = ""
    call_id: str = ""


class ABEvalReportOut(BaseModel):
    id: str
    run_id_a: str
    run_id_b: str
    question_id_a: str = ""
    question_id_b: str = ""
    question_headline: str = ""
    overall_assessment: str
    overall_assessment_call_id: str = ""
    eval_run_id: str = ""
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
    child_questions: int
    considerations: int
    judgements: int
    views: int


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


class PrioritizationCandidateOut(BaseModel):
    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    call_id: str
    run_id: str
    scope_page_id: str
    scope_headline: str
    created_at: datetime
    is_scope: bool


class QuestionStatsOut(StatsOut):
    question_id: str
    subgraph_page_count: int
    subgraph: Subgraph
    prioritization_candidates: list[PrioritizationCandidateOut] = []


class PageLoadEventOut(BaseModel):
    page_id: str
    detail: str
    tags: dict[str, str]


class PageLoadStatsOut(BaseModel):
    events: list[PageLoadEventOut]
    total: int
    total_unique: int
    question_headlines: dict[str, str]
