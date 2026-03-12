"""
Pydantic schemas for the API layer.

Composite response types and trace event envelope types. Core models
(Page, PageLink, Call, Project) live in differential.models.
"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from differential.models import Call, Page, PageLink
from differential.tracing.trace_events import (
    ContextBuiltEvent,
    DispatchesPlannedEvent,
    DispatchExecutedEvent,
    ErrorEvent,
    LLMExchangeEvent,
    MovesExecutedEvent,
    Phase1LoadedEvent,
    Phase2LoadedEvent,
    ReviewCompleteEvent,
    WarningEvent,
)


class LinkedPageOut(BaseModel):
    page: Page
    link: PageLink


class PageDetailOut(BaseModel):
    page: Page
    links_from: list[LinkedPageOut]
    links_to: list[LinkedPageOut]


class ConsiderationOut(BaseModel):
    page: Page
    link: PageLink


class QuestionTreeOut(BaseModel):
    question: Page
    considerations: list[ConsiderationOut]
    judgements: list[Page]
    child_questions: list['QuestionTreeOut']


class PageCountsOut(BaseModel):
    considerations: int
    judgements: int


class _TraceEnvelopeMixin(BaseModel):
    ts: str
    call_id: str


class ContextBuiltEventOut(ContextBuiltEvent, _TraceEnvelopeMixin):
    event: Literal["context_built"]


class Phase1LoadedEventOut(Phase1LoadedEvent, _TraceEnvelopeMixin):
    event: Literal["phase1_loaded"]


class Phase2LoadedEventOut(Phase2LoadedEvent, _TraceEnvelopeMixin):
    event: Literal["phase2_loaded"]


class MovesExecutedEventOut(MovesExecutedEvent, _TraceEnvelopeMixin):
    event: Literal["moves_executed"]


class ReviewCompleteEventOut(ReviewCompleteEvent, _TraceEnvelopeMixin):
    event: Literal["review_complete"]


class LLMExchangeEventOut(LLMExchangeEvent, _TraceEnvelopeMixin):
    event: Literal["llm_exchange"]


class WarningEventOut(WarningEvent, _TraceEnvelopeMixin):
    event: Literal["warning"]


class ErrorEventOut(ErrorEvent, _TraceEnvelopeMixin):
    event: Literal["error"]


class DispatchesPlannedEventOut(DispatchesPlannedEvent, _TraceEnvelopeMixin):
    event: Literal["dispatches_planned"]


class DispatchExecutedEventOut(DispatchExecutedEvent, _TraceEnvelopeMixin):
    event: Literal["dispatch_executed"]


TraceEventOut = Annotated[
    ContextBuiltEventOut
    | Phase1LoadedEventOut
    | Phase2LoadedEventOut
    | MovesExecutedEventOut
    | ReviewCompleteEventOut
    | LLMExchangeEventOut
    | WarningEventOut
    | ErrorEventOut
    | DispatchesPlannedEventOut
    | DispatchExecutedEventOut,
    Field(discriminator="event"),
]


class LLMExchangeSummaryOut(BaseModel):
    id: str
    phase: str
    round: int
    input_tokens: int | None
    output_tokens: int | None
    duration_ms: int | None
    error: str | None
    created_at: datetime


class LLMExchangeOut(BaseModel):
    id: str
    call_id: str
    phase: str
    round: int
    system_prompt: str | None
    user_message: str | None
    response_text: str | None
    tool_calls: list[dict]
    input_tokens: int | None
    output_tokens: int | None
    duration_ms: int | None
    error: str | None
    created_at: datetime


class CallTraceOut(BaseModel):
    call: Call
    scope_page_summary: str | None = None
    events: list[TraceEventOut]
    children: list['CallTraceOut']


class RunTraceOut(BaseModel):
    run_id: str
    question: Page | None
    root_calls: list[CallTraceOut]


class RunSummaryOut(BaseModel):
    run_id: str
    created_at: str


class RealtimeConfigOut(BaseModel):
    url: str
    anon_key: str
