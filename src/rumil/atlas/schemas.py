"""Pydantic schemas served by the /atlas API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class JsonSchemaField(BaseModel):
    """One field of a payload schema, projected for the atlas UI."""

    name: str
    type: str
    description: str = ""
    required: bool = False
    default: Any | None = None
    enum: list[str] | None = None
    items_type: str | None = None
    items_ref: str | None = None
    minimum: float | None = None
    maximum: float | None = None


class MoveSummary(BaseModel):
    move_type: str
    name: str
    description: str
    fields: list[JsonSchemaField]
    used_in_call_types: list[str]
    used_in_presets: list[str]
    code_path: str
    raw_schema: dict | None = None


class DispatchSummary(BaseModel):
    call_type: str
    name: str
    description: str
    fields: list[JsonSchemaField]
    is_recurse: bool = False
    raw_schema: dict | None = None


class PromptSection(BaseModel):
    """One ## section within a prompt file."""

    title: str
    level: int
    anchor: str
    body: str
    char_count: int
    applies_to: list[str] = []
    applies_to_note: str | None = None


class PromptPart(BaseModel):
    """One file's contribution to a system-prompt composition."""

    name: str
    role: str
    location: str = "system"
    condition: str | None = None
    optional: bool = False
    char_count: int = 0
    sections: list[PromptSection] = []
    exists: bool = True


class PromptComposition(BaseModel):
    """Ordered prompt parts that compose into a call's system prompt."""

    call_type: str
    parts: list[PromptPart]
    total_chars: int


class CallTypeSummary(BaseModel):
    call_type: str
    description: str
    has_dispatch: bool
    dispatch_name: str | None = None
    prompt_files: list[str]
    moves_by_preset: dict[str, list[str]]
    runner_class: str | None = None
    context_builder: str | None = None
    workspace_updater: str | None = None
    closing_reviewer: str | None = None
    composition: PromptComposition | None = None


class PageTypeSummary(BaseModel):
    page_type: str
    description: str
    layer_hint: str | None = None


class EnumSummary(BaseModel):
    name: str
    value: str
    description: str = ""


class WorkflowStage(BaseModel):
    """One stage in a workflow's spec — what runs, when, and with what."""

    id: str
    label: str
    description: str = ""
    prompt_files: list[str] = []
    available_dispatch_call_types: list[str] = []
    available_move_types: list[str] = []
    optional: bool = False
    branch_condition: str | None = None
    loop: bool = False
    recurses_into: list[str] = []
    note: str | None = None


class WorkflowSummary(BaseModel):
    name: str
    kind: str
    summary: str
    code_paths: list[str] = []


class WorkflowProfile(BaseModel):
    name: str
    kind: str
    summary: str
    code_paths: list[str] = []
    relevant_settings: list[str] = []
    stages: list[WorkflowStage] = []
    recurses_into: list[str] = []
    fingerprint_keys: list[str] = []
    notes: list[str] = []


class RegistryRollup(BaseModel):
    """Top-level /atlas/registry response — counts + summaries for the index."""

    n_moves: int
    n_dispatches: int
    n_call_types: int
    n_page_types: int
    n_workflows: int
    n_prompt_files: int
    move_summaries: list[MoveSummary]
    dispatch_summaries: list[DispatchSummary]
    call_type_summaries: list[CallTypeSummary]
    page_type_summaries: list[PageTypeSummary]
    workflow_summaries: list[WorkflowSummary]
    presets: dict[str, list[str]]
    available_calls_presets: list[str]


class PromptDoc(BaseModel):
    """A prompt markdown file rendered for the atlas UI."""

    name: str
    path: str
    content: str
    char_count: int
    content_hash: str = ""
    referenced_by: list[str] = []
    sections: list[PromptSection] = []
    used_in_compositions: list[str] = []


class PromptHistoryEntry(BaseModel):
    """One git commit that touched a prompt file.

    ``content_hash`` is sha-256 of the file as it stood at this commit
    (not the git blob hash) — same shape as the live file's
    ``PromptDoc.content_hash`` so an iterator can spot-check whether a
    particular run's prompt matched any of the historical revisions.
    """

    commit_sha: str
    commit_short: str
    commit_ts: str
    author: str = ""
    subject: str = ""
    content_hash: str
    char_count: int = 0


class PromptHistory(BaseModel):
    name: str
    path: str
    current_content_hash: str
    entries: list[PromptHistoryEntry]
    truncated: bool = False


class RunOutcome(BaseModel):
    """Coarse "did this run succeed at its job" signal.

    Pulled from runs.config.outcome when present (e.g. eval workflows
    write a verdict there); else derived heuristically from run state —
    completed-without-error vs aborted vs noop. UI can show as a small
    badge per run.
    """

    label: str
    score: float | None = None
    source: str = "heuristic"
    detail: str = ""


class RunRollup(BaseModel):
    """Per-run summary used in workflow aggregate views."""

    run_id: str
    created_at: str
    name: str = ""
    question_id: str | None = None
    question_headline: str | None = None
    n_calls: int = 0
    n_dispatches: int = 0
    n_pages_loaded: int = 0
    cost_usd: float = 0.0
    duration_seconds: float | None = None
    last_status: str | None = None
    is_noop: bool = False
    n_llm_exchanges: int = 0
    stages_taken: list[str] = []
    stages_skipped: list[str] = []
    dispatch_counts: dict[str, int] = {}
    call_status_counts: dict[str, int] = {}
    outcome: RunOutcome | None = None
    n_judgements_created: int = 0
    n_views_created: int = 0
    n_questions_created: int = 0


class GapItem(BaseModel):
    """One detected inconsistency surfaced on /atlas/gaps."""

    kind: str
    target: str
    detail: str = ""
    href: str | None = None


class GapsReport(BaseModel):
    items: list[GapItem]
    counts_by_kind: dict[str, int]


class SearchHit(BaseModel):
    kind: str
    id: str
    title: str
    snippet: str = ""
    score: float = 0.0
    href: str | None = None


class SearchResults(BaseModel):
    query: str
    hits: list[SearchHit]
    total: int
    by_kind: dict[str, int]


class WorkflowGraphNode(BaseModel):
    id: str
    label: str
    kind: str
    standalone: bool = False


class WorkflowGraphEdge(BaseModel):
    from_id: str
    to_id: str
    via_stage: str | None = None


class WorkflowGraph(BaseModel):
    nodes: list[WorkflowGraphNode]
    edges: list[WorkflowGraphEdge]


class OverlayCall(BaseModel):
    call_id: str
    call_type: str
    status: str
    cost_usd: float = 0.0
    pages_loaded: int = 0
    n_dispatches: int = 0
    started_at: str | None = None
    duration_seconds: float | None = None


class WorkflowOverlayStage(BaseModel):
    stage_id: str
    label: str
    fired: bool = False
    skipped: bool = False
    skipped_reason: str | None = None
    iterations: int = 0
    calls: list[OverlayCall] = []
    cost_usd: float = 0.0
    pages_loaded: int = 0


class PageCallRef(BaseModel):
    call_id: str
    call_type: str
    run_id: str = ""
    role: str
    created_at: str = ""
    cost_usd: float = 0.0
    status: str = ""


class PageInstanceCalls(BaseModel):
    page_id: str
    page_type: str
    headline: str = ""
    created_by_call: PageCallRef | None = None
    in_context_of: list[PageCallRef] = []
    loaded_by: list[PageCallRef] = []
    superseded_by_page_id: str | None = None


class PageTimelineEvent(BaseModel):
    ts: str
    kind: str
    call_id: str | None = None
    call_type: str | None = None
    run_id: str | None = None
    detail: str = ""


class PageTimeline(BaseModel):
    page_id: str
    page_type: str
    headline: str = ""
    events: list[PageTimelineEvent] = []


class StageDiffRow(BaseModel):
    stage_id: str
    label: str
    a_fired: bool = False
    b_fired: bool = False
    a_skipped: bool = False
    b_skipped: bool = False
    a_iterations: int = 0
    b_iterations: int = 0
    a_cost_usd: float = 0.0
    b_cost_usd: float = 0.0
    a_pages_loaded: int = 0
    b_pages_loaded: int = 0
    a_n_calls: int = 0
    b_n_calls: int = 0


class DispatchCountDiff(BaseModel):
    call_type: str
    a_count: int = 0
    b_count: int = 0


class RunDiffSide(BaseModel):
    run_id: str
    name: str = ""
    workflow_name: str | None = None
    cost_usd: float = 0.0
    n_calls: int = 0
    n_dispatches: int = 0
    pages_loaded: int = 0
    duration_seconds: float | None = None
    started_at: str | None = None


class RunDiff(BaseModel):
    a: RunDiffSide
    b: RunDiffSide
    same_workflow: bool
    aligned_workflow: str | None = None
    stages: list[StageDiffRow]
    dispatch_diffs: list[DispatchCountDiff]
    notes: list[str] = []


class WorkflowOverlay(BaseModel):
    workflow_name: str
    run_id: str
    profile: WorkflowProfile
    stages: list[WorkflowOverlayStage]
    n_calls: int = 0
    cost_usd: float = 0.0
    duration_seconds: float | None = None
    started_at: str | None = None
    finished_at: str | None = None


class LiveRunSnapshot(BaseModel):
    """Snapshot of an in-flight (or recent) run for the live overlay UI.

    A subset of WorkflowOverlay plus a few in-flight signals: the most
    recent trace event timestamp, whether any call is still pending or
    running, and a guessed "current stage" for highlighting.
    """

    run_id: str
    workflow_name: str | None = None
    is_in_flight: bool = False
    last_event_ts: str | None = None
    current_stage_id: str | None = None
    overlay: WorkflowOverlay | None = None
    n_pending_calls: int = 0
    n_running_calls: int = 0
    snapshot_ts: str


class MoveCount(BaseModel):
    move_type: str
    count: int


class CoFiringCount(BaseModel):
    a: str
    b: str
    count: int


class CallTypeInvocationCount(BaseModel):
    call_type: str
    count: int


class HistogramBin(BaseModel):
    label: str
    lo: float
    hi: float
    count: int


class StatsBucket(BaseModel):
    bucket_start: str
    bucket_end: str
    n_invocations: int
    mean_cost_usd: float = 0.0
    total_cost_usd: float = 0.0
    mean_rounds: float = 0.0


class PathologyCounts(BaseModel):
    """Frequency of failure / instability patterns across a call type's
    recent invocations. Each `*_pct` is a 0-100 fraction of the call
    type's invocations exhibiting the pattern; `n_error_events` is a
    count of trace ErrorEvents across all invocations.

    A "lying COMPLETE" call is one with status=complete that nonetheless
    emitted an ErrorEvent in its trace — the canonical silent-failure
    pattern flagged by the open-issues mining pass.
    """

    n_error_events: int = 0
    error_pct: float = 0.0
    lying_complete_pct: float = 0.0
    rounds_capped_pct: float = 0.0
    parse_fail_pct: float = 0.0
    truncated_pct: float = 0.0


class CallTypeStats(BaseModel):
    """Empirical stats for one CallType across recent runs."""

    call_type: str
    scanned_runs: int
    runs_with_call: int
    n_invocations: int
    mean_cost_usd: float = 0.0
    total_cost_usd: float = 0.0
    mean_pages_loaded: float = 0.0
    mean_rounds: float = 0.0
    status_counts: dict[str, int] = {}
    top_moves: list[MoveCount] = []
    top_co_firings: list[CoFiringCount] = []
    recent_errors: list[str] = []
    p50_cost_usd: float = 0.0
    p90_cost_usd: float = 0.0
    p99_cost_usd: float = 0.0
    rounds_histogram: list[HistogramBin] = []
    cost_histogram: list[HistogramBin] = []
    pages_loaded_histogram: list[HistogramBin] = []
    series: list[StatsBucket] = []
    bucket: str | None = None
    since: str | None = None
    pathology: PathologyCounts = PathologyCounts()


class MoveStats(BaseModel):
    """Empirical stats for one MoveType across recent runs."""

    move_type: str
    scanned_runs: int
    runs_with_move: int
    n_invocations: int
    invocations_by_call_type: list[CallTypeInvocationCount] = []
    last_seen: str | None = None


class StageInvocation(BaseModel):
    stage_id: str
    label: str
    taken_count: int
    skipped_count: int
    total_runs: int


class DispatchFrequency(BaseModel):
    call_type: str
    total: int
    avg_per_run: float
    runs_with_at_least_one: int


class WorkflowAggregate(BaseModel):
    workflow_name: str
    n_runs: int
    runs: list[RunRollup]
    stage_invocations: list[StageInvocation]
    dispatch_frequencies: list[DispatchFrequency]
    pages_loaded_per_run: list[int] = []
    cost_per_run: list[float] = []
    dispatches_per_run: list[int] = []
    calls_per_run: list[int] = []
    sparkline: list[float] = []


class RunFlow(BaseModel):
    """Single run's flow — calls in order with which workflow stage they
    correspond to (best-effort) and what they did."""

    run_id: str
    workflow_name: str | None = None
    nodes: list[RunFlowNode] = []


class RunFlowNode(BaseModel):
    call_id: str
    parent_call_id: str | None = None
    call_type: str
    call_type_description: str = ""
    status: str
    cost_usd: float = 0.0
    pages_loaded: int = 0
    n_dispatches: int = 0
    started_at: str | None = None
    duration_seconds: float | None = None
    stage_id: str | None = None
    summary: str = ""
    closing_review_outcome: str | None = None
    has_error_event: bool = False
    n_llm_exchanges: int = 0


class TraceEventRecord(BaseModel):
    """One trace event lifted from a call's ``trace_json``.

    The ``payload`` dict holds the event's full body except the
    discriminator ``event`` field (which lives on ``kind``); shape is
    arbitrary so the FE can render typed events richly.
    """

    index: int
    kind: str
    payload: dict


class RenderedPromptSample(BaseModel):
    """A real LLM exchange surfaced as a rendered-prompt sample.

    The raw text of the system prompt + user messages as the model
    actually saw them. Used to catch ``{{TASK}}`` leaks, parent-headline
    pollution, and silent default mismatches that the
    static composition view can't detect (since atlas's composition
    only shows the template, not the substituted text).

    ``anomalies`` lists detected pattern matches the FE can highlight —
    today: literal ``{{...}}`` token survivors, fallback-task placeholder
    leak, missing preamble.
    """

    exchange_id: str
    call_id: str
    call_type: str
    run_id: str = ""
    created_at: str = ""
    model: str = ""
    phase: str = ""
    round: int | None = None
    system_prompt: str = ""
    user_message: str = ""
    response_text: str = ""
    has_error: bool = False
    anomalies: list[str] = []


class ExchangeSearchHit(BaseModel):
    exchange_id: str
    call_id: str
    call_type: str
    run_id: str = ""
    created_at: str = ""
    field: str
    snippet: str
    score: float = 1.0


class ExchangeSearchResults(BaseModel):
    query: str
    hits: list[ExchangeSearchHit]
    total: int
    truncated: bool = False
    n_scanned: int


class CallEventDump(BaseModel):
    call_id: str
    call_type: str
    status: str
    n_events: int
    events: list[TraceEventRecord]
    n_error_events: int
    n_llm_exchanges: int
    closing_review_outcome: str | None = None


RunFlow.model_rebuild()
