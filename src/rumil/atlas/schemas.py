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
    referenced_by: list[str] = []
    sections: list[PromptSection] = []
    used_in_compositions: list[str] = []


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
    stages_taken: list[str] = []
    stages_skipped: list[str] = []
    dispatch_counts: dict[str, int] = {}
    call_status_counts: dict[str, int] = {}


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


RunFlow.model_rebuild()
