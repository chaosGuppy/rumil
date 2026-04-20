"""
Pydantic schemas for the API layer.

Composite response types and trace event envelope types. Core models
(Page, PageLink, Call, Project) live in rumil.models.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from rumil.calls.adversarial_review import StrongerSide
from rumil.models import Page, PageLink, Project, _all_fields_required
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
    NudgeAppliedEvent,
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


class ViewItemOut(BaseModel):
    """One item inside a QuestionView section — a page plus the links that
    connect it to the question (or the surrounding question subgraph)."""

    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    page: Page
    links: list[PageLink]
    section: str


class ViewSectionOut(BaseModel):
    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    name: str
    description: str
    items: list[ViewItemOut]


class ViewHealthOut(BaseModel):
    """Aggregate health signals on a QuestionView — how much research exists
    and where the obvious gaps are."""

    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    total_pages: int
    missing_credence: int
    missing_importance: int
    child_questions_without_judgements: int
    max_depth: int


class QuestionViewOut(BaseModel):
    """Response model for GET /api/questions/{id}/view."""

    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    question: Page
    sections: list[ViewSectionOut]
    health: ViewHealthOut


# Background-task launcher responses. Each POST that kicks off an async
# run returns 202 with a run_id the caller uses to open /traces/{run_id}.
# Giving them real response models means frontends and skills consume
# typed fields instead of `unknown`.


class ContinueQuestionOut(BaseModel):
    """POST /api/questions/{id}/continue response."""

    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    run_id: str
    question_id: str
    budget: int


class EvaluateQuestionOut(BaseModel):
    """POST /api/questions/{id}/evaluate response."""

    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    run_id: str
    question_id: str
    eval_type: str


class DispatchCallOut(BaseModel):
    """POST /api/questions/{id}/dispatch response.

    Fires a single dispatchable call type (find_considerations, assess,
    scout_*, web_research, etc.) on the question in the background. The
    run_id is the fresh trace run — client navigates to /traces/{run_id}
    to watch.
    """

    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    run_id: str
    question_id: str
    call_type: str
    max_rounds: int


class GroundCallOut(BaseModel):
    """POST /api/calls/{id}/ground and /api/calls/{id}/feedback response.

    ``pipeline`` is ``"grounding"`` or ``"feedback"`` — the two grounding
    pipelines registered in rumil.evaluate.registry.
    """

    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    run_id: str
    source_call_id: str
    pipeline: str
    from_stage: int


class StageRunOut(BaseModel):
    """POST /api/runs/{run_id}/stage and .../commit response.

    ``staged`` is True after /stage, False after /commit.
    """

    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    run_id: str
    staged: bool


class ABEvalStartedOut(BaseModel):
    """POST /api/ab-evals response. The actual ab_eval_report id is only
    known when the background eval completes; callers poll
    /api/ab-evals and filter by run_id_a/b until the report appears."""

    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    run_id_a: str
    run_id_b: str
    status: str


class OrchestratorSpecOut(BaseModel):
    """One entry in CapabilitiesOut.orchestrators. Mirrors
    rumil.orchestrators.registry.OrchestratorSpec's public shape — the
    factory callable is intentionally omitted."""

    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    variant: str
    description: str
    stability: str
    cost_band: str
    exposed_in_chat: bool
    supports_global_prio: bool


class EvaluationTypeSpecOut(BaseModel):
    """One entry in CapabilitiesOut.eval_types. Mirrors
    rumil.evaluate.registry.EvaluationTypeSpec."""

    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    name: str
    description: str
    prompt_file: str
    investigator_prompt_file: str


class GroundingPipelineSpecOut(BaseModel):
    """One entry in CapabilitiesOut.grounding_pipelines. Mirrors the public
    fields on rumil.evaluate.registry.GroundingPipelineSpec (runner is
    omitted)."""

    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    name: str
    description: str
    recommended_eval_type: str


class CallTypeInfoOut(BaseModel):
    """One entry in CapabilitiesOut.call_types. ``dispatchable`` indicates
    whether prioritization is allowed to dispatch this call type (i.e. the
    value is in ``DISPATCHABLE_CALL_TYPES``)."""

    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    value: str
    dispatchable: bool


class PhaseOut(BaseModel):
    """One step in an orchestrator's execution pattern.

    For PolicyOrchestrator-based orchestrators, this is derived directly
    from the live ``Policy.name`` / ``Policy.description`` attributes —
    so the phase list can't drift from the composition. For hand-coded
    orchestrators, ``name`` is absent and ``description`` is the
    free-form step text from ``OrchestratorSpec.static_phases``.
    """

    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    name: str | None
    description: str
    source: str  # "policy" (derived from code) or "static" (hand-written in registry)


class RelatedCallTypeOut(BaseModel):
    """A call type linked from an orchestrator info page.

    ``description`` is the one-liner from ``rumil.descriptions`` —
    shown as a tooltip / expandable row on the orch info popover.
    """

    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    value: str
    description: str


class ObservedBehaviorOut(BaseModel):
    """Histogram of call types across recent runs of an orchestrator.

    Lets the UI cross-check the written description against what the
    orchestrator actually dispatches — the strongest drift detector.
    ``run_count`` is the number of runs the histogram was computed over.
    """

    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    run_count: int
    call_type_counts: dict[str, int]


class OrchestratorInfoOut(BaseModel):
    """GET /api/orchestrators/{variant} — detailed info for the popover.

    Superset of OrchestratorSpecOut. Adds overview, phases (derived or
    static), mermaid diagram, related call types, and observed behavior
    stats."""

    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    variant: str
    description: str
    stability: str
    cost_band: str
    exposed_in_chat: bool
    supports_global_prio: bool
    overview: str
    diagram_mermaid: str
    phases: list[PhaseOut]
    related_call_types: list[RelatedCallTypeOut]
    observed_behavior: ObservedBehaviorOut


class CapabilitiesOut(BaseModel):
    """GET /api/capabilities response.

    Aggregates everything the frontend/chat/skills need to render pickers
    and catalogs without hardcoding lists. Sourced entirely from the Python
    registries so new variants appear here the moment they're registered.
    """

    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    orchestrators: list[OrchestratorSpecOut]
    eval_types: list[EvaluationTypeSpecOut]
    grounding_pipelines: list[GroundingPipelineSpecOut]
    call_types: list[CallTypeInfoOut]
    available_calls_presets: list[str]
    available_moves_presets: list[str]


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


# Workspace names are short human labels. Cap at 80 chars to match what fits
# on the landing card without wrapping and to keep the DB column predictable.
_PROJECT_NAME_MAX = 80


class CreateProjectRequest(BaseModel):
    """POST /api/projects body.

    The name is trimmed server-side; a purely-whitespace or empty value is
    rejected with 422 via the ``min_length`` validator.
    """

    name: Annotated[
        str,
        Field(min_length=1, max_length=_PROJECT_NAME_MAX),
    ]


class CreateProjectOut(BaseModel):
    """POST /api/projects response.

    ``created`` is ``True`` when a new row was inserted, ``False`` when an
    existing workspace with the same name was returned. The frontend uses
    this to decide whether to show a subtle "already exists" hint.
    """

    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    project: Project
    created: bool


class UpdateProjectRequest(BaseModel):
    """PATCH /api/projects/{id} body.

    All fields optional — callers send only what they want to change. ``name``
    is trimmed server-side and validated for length; ``hidden`` is a straight
    boolean flag that removes the workspace from the default public list.
    """

    name: Annotated[
        str | None,
        Field(default=None, min_length=1, max_length=_PROJECT_NAME_MAX),
    ] = None
    hidden: bool | None = None


class UpdateRunRequest(BaseModel):
    """PATCH /api/runs/{run_id} body.

    Runs only surface the ``hidden`` toggle today — a hidden run is filtered
    out of the RunPicker by default but still readable via a show-hidden
    affordance.
    """

    hidden: bool | None = None


# Cap the headline around the "10-15 words, 20-word ceiling" guidance that
# HEADLINE_DESCRIPTION in moves/base.py uses for LLM-created questions —
# 300 chars leaves plenty of slack for humans without letting the column grow
# unbounded. Content is a larger freeform body.
_QUESTION_HEADLINE_MAX = 300
_QUESTION_CONTENT_MAX = 20000


class CreateRootQuestionRequest(BaseModel):
    """POST /api/projects/{project_id}/questions body.

    The headline is trimmed server-side; empty or whitespace-only input is
    rejected with 422. ``content`` is optional — if omitted the question is
    created with the headline as its content (matches the skill-lane pattern
    in ``ask_question.py``).
    """

    headline: Annotated[
        str,
        Field(min_length=1, max_length=_QUESTION_HEADLINE_MAX),
    ]
    content: Annotated[
        str | None,
        Field(default=None, max_length=_QUESTION_CONTENT_MAX),
    ] = None


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


class NudgeAppliedEventOut(NudgeAppliedEvent, _TraceEnvelopeMixin):
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
    | UpdateViewPhaseCompletedEventOut
    | NudgeAppliedEventOut,
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
    # Content hash of the composite system prompt used in this exchange, or
    # null for legacy exchanges that predate prompt versioning. The UI shows
    # the first 12 chars as a monospace tag.
    composite_prompt_hash: str | None = None
    # Friendly prompt name (e.g. "big_assess", "composite") resolved from
    # prompt_versions.name on read. Falls back to "composite" when the
    # exchange has a hash but no matching prompt_versions row.
    prompt_name: str | None = None


class LLMExchangeOut(BaseModel):
    model_config = ConfigDict(json_schema_extra=_all_fields_required)

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
    composite_prompt_hash: str | None = None
    prompt_name: str | None = None


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
    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    call: CallSummary
    scope_page_summary: str | None = None
    warning_count: int = 0
    error_count: int = 0


class RunTraceTreeOut(BaseModel):
    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    run_id: str
    question: Page | None
    calls: list[CallNodeOut]
    cost_usd: float | None = None
    staged: bool = False
    config: dict = {}


class RunSummaryOut(BaseModel):
    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    run_id: str
    created_at: str
    provenance_call_id: str = ""


class RunListItemOut(BaseModel):
    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    run_id: str | None = None
    created_at: str
    name: str = ""
    config: dict | None = None
    question_summary: str | None = None
    staged: bool = False
    hidden: bool = False


class RunSpendByCallTypeOut(BaseModel):
    """One row of the per-call-type spend breakdown for a run."""

    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    call_type: str
    count: int
    cost_usd: float
    duration_ms: int


class RunSpendOut(BaseModel):
    """Aggregate spend for a single run, broken down by call_type.

    ``total_duration_ms`` sums ``completed_at - created_at`` across the run's
    calls (only counting calls that have actually completed); ``cost_usd``
    sums ``cost_usd`` across all calls. The ``by_call_type`` list is sorted
    descending by ``cost_usd``.
    """

    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    run_id: str
    run_id_short: str
    total_cost_usd: float
    total_duration_ms: int
    total_calls: int
    by_call_type: list[RunSpendByCallTypeOut]


class RefineIterationVerdictOut(BaseModel):
    """Verdict summary attached to a refine-artifact iteration.

    Pulled from ``extra['adversarial_verdict']`` on the JUDGEMENT page that
    reviewed the draft. Named after the raw payload shape so the frontend
    diff panel can render a chip like "claim_holds at conf 6, 2 dissents".
    """

    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    claim_holds: bool
    claim_confidence: int
    dissents: list[str]
    concurrences: list[str]
    stronger_side: StrongerSide


class RefineIterationOut(BaseModel):
    """One iteration in a refine-artifact chain (draft + optional verdict)."""

    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    iteration: int
    draft_page_id: str
    draft_short_id: str
    content: str
    headline: str
    verdict: RefineIterationVerdictOut | None
    created_at: datetime


class PageIterationsOut(BaseModel):
    """Response for GET /api/pages/{page_id}/iterations — ordered v1->vN."""

    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    page_id: str
    iterations: list[RefineIterationOut]


class PaginatedPagesOut(BaseModel):
    model_config = ConfigDict(json_schema_extra=_all_fields_required)
    items: Sequence[Page]
    total_count: int
    offset: int
    limit: int


class SearchResultOut(BaseModel):
    """One hit in a workspace full-text search.

    ``snippet`` is an ~200 char window of ``page.content`` around the first
    match of the query (or the leading prefix if the match is in the
    headline only). Case-insensitive ILIKE across headline and content.
    """

    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    page: Page
    snippet: str


class SearchResultsOut(BaseModel):
    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    results: list[SearchResultOut]


class ABEvalDimensionOut(BaseModel):
    model_config = ConfigDict(json_schema_extra=_all_fields_required)

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
    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    id: str
    run_id_a: str
    run_id_b: str
    question_id_a: str = ""
    question_id_b: str = ""
    question_headline: str = ""
    overall_assessment: str
    overall_assessment_call_id: str = ""
    eval_run_id: str = ""
    """Run ID that produced the comparison / overall-assessment calls.

    Distinct from ``run_id_a`` / ``run_id_b`` (the runs being
    compared) — this is the eval's own run, which the trace UI
    links to when rendering dimension + overall-assessment call
    traces.
    """
    dimension_reports: list[ABEvalDimensionOut]
    config_a: dict = {}
    config_b: dict = {}
    created_at: str


class ABEvalDimensionSummaryOut(BaseModel):
    name: str
    display_name: str
    preference: str


class ABEvalReportListItemOut(BaseModel):
    model_config = ConfigDict(json_schema_extra=_all_fields_required)

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
    # Per-question page counts for the focus-bars UI. Default 0 because
    # the current stats RPC (compute_project_stats / compute_question_stats)
    # doesn't populate them yet — extending the SQL is a follow-up. Keeping
    # the fields on the schema lets the TS frontend render the UI immediately
    # without a type error, with zero bars until the RPC is updated.
    child_questions: int = 0
    considerations: int = 0
    judgements: int = 0


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
    question_headlines: dict[str, str]


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
    stronger_side: StrongerSide
    claim_holds: bool
    confidence: int
    rationale: str
    concurrences: list[str]
    dissents: list[str]
    sunset_after_days: int | None
    verdict_created_at: datetime
    expired: bool
    page_created_at: datetime


class LlmBoundaryExchangeListItemOut(BaseModel):
    """One row in the boundary-exchanges list for a workspace.

    Compact view: the heavy ``request_json`` / ``response_json`` columns
    are omitted; clients fetch them on demand via the detail endpoint.
    """

    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    id: str
    project_id: str | None
    run_id: str | None
    call_id: str | None
    started_at: datetime
    finished_at: datetime | None
    latency_ms: int | None
    model: str
    usage: dict[str, Any] | None
    stop_reason: str | None
    error_class: str | None
    error_message: str | None
    http_status: int | None
    source: str
    streamed: bool


class PaginatedLlmBoundaryExchangesOut(BaseModel):
    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    items: Sequence[LlmBoundaryExchangeListItemOut]
    total_count: int
    offset: int
    limit: int


class LlmBoundaryExchangeDetailOut(BaseModel):
    """Full row including the verbatim request_json and response_json."""

    model_config = ConfigDict(json_schema_extra=_all_fields_required)

    id: str
    project_id: str | None
    run_id: str | None
    call_id: str | None
    started_at: datetime
    finished_at: datetime | None
    latency_ms: int | None
    model: str
    request_json: dict[str, Any]
    response_json: dict[str, Any] | None
    usage: dict[str, Any] | None
    stop_reason: str | None
    error_class: str | None
    error_message: str | None
    http_status: int | None
    source: str
    streamed: bool
    created_at: datetime
