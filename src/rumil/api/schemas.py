"""
Pydantic schemas for the API layer.

Composite response types and trace event envelope types. Core models
(Page, PageLink, Call, Project) live in rumil.models.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from rumil.models import Call, Page, PageLink, _all_fields_required
from rumil.tracing.trace_events import (
    ContextBuiltEvent,
    DispatchesPlannedEvent,
    DispatchExecutedEvent,
    ErrorEvent,
    ExplorePageEvent,
    LLMExchangeEvent,
    MovesExecutedEvent,
    ReviewCompleteEvent,
    ScoringCompletedEvent,
    SubagentCompletedEvent,
    SubagentStartedEvent,
    WarningEvent,
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
    | SubagentCompletedEventOut,
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


class CallSequenceOut(BaseModel):
    id: str
    position_in_batch: int
    calls: Sequence["CallTraceOut"]


class CallTraceOut(BaseModel):
    call: Call
    scope_page_summary: str | None = None
    events: list[TraceEventOut]
    children: list["CallTraceOut"]
    sequences: Sequence[CallSequenceOut] | None = None
    cost_usd: float | None = None


class RunTraceOut(BaseModel):
    run_id: str
    question: Page | None
    root_calls: list[CallTraceOut]
    cost_usd: float | None = None


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
    ab_run_id: str | None = None
    arms: dict | None = None


class ABRunArmOut(BaseModel):
    run_id: str
    name: str = ""
    config: dict = {}
    trace: RunTraceOut


class ABRunTraceOut(BaseModel):
    ab_run_id: str
    name: str = ""
    question: Page | None = None
    arms: list[ABRunArmOut]


class RealtimeConfigOut(BaseModel):
    url: str
    anon_key: str
