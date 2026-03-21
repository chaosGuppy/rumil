"""
Pydantic schemas for the API layer.

Composite response types and trace event envelope types. Core models
(Page, PageLink, Call, Project) live in rumil.models.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field

from rumil.models import Call, Page, PageLink
from rumil.tracing.trace_events import (
    ContextBuiltEvent,
    DispatchesPlannedEvent,
    DispatchExecutedEvent,
    ErrorEvent,
    LLMExchangeEvent,
    MovesExecutedEvent,
    ReviewCompleteEvent,
    ScoringCompletedEvent,
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


TraceEventOut = Annotated[
    ContextBuiltEventOut
    | MovesExecutedEventOut
    | ReviewCompleteEventOut
    | LLMExchangeEventOut
    | WarningEventOut
    | ErrorEventOut
    | ScoringCompletedEventOut
    | DispatchesPlannedEventOut
    | DispatchExecutedEventOut,
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
    calls: Sequence['CallTraceOut']


class CallTraceOut(BaseModel):
    call: Call
    scope_page_summary: str | None = None
    events: list[TraceEventOut]
    children: list['CallTraceOut']
    sequences: Sequence[CallSequenceOut] | None = None
    cost_usd: float | None = None


class RunTraceOut(BaseModel):
    run_id: str
    question: Page | None
    root_calls: list[CallTraceOut]
    cost_usd: float | None = None


class RunSummaryOut(BaseModel):
    run_id: str
    created_at: str


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
