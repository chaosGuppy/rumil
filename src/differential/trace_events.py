"""Typed trace event models for the execution tracer."""

from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, Field


class PageRef(BaseModel):
    id: str
    summary: str = ""


def _coerce_page_refs(v: list) -> list:
    """Accept both bare ID strings (legacy traces) and PageRef dicts."""
    return [{"id": x} if isinstance(x, str) else x for x in v]


PageRefList = Annotated[list[PageRef], BeforeValidator(_coerce_page_refs)]


class MoveTraceItem(BaseModel):
    type: str
    summary: str = ""
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
    duration_ms: int | None = None


class WarningEvent(BaseModel):
    event: Literal["warning"] = "warning"
    message: str


class ErrorEvent(BaseModel):
    event: Literal["error"] = "error"
    message: str


class DispatchesPlannedEvent(BaseModel):
    event: Literal["dispatches_planned"] = "dispatches_planned"
    dispatches: list[DispatchTraceItem] = []


class DispatchExecutedEvent(BaseModel):
    event: Literal["dispatch_executed"] = "dispatch_executed"
    index: int
    child_call_type: str
    question_id: str
    child_call_id: str | None = None


TraceEvent = Annotated[
    ContextBuiltEvent
    | MovesExecutedEvent
    | ReviewCompleteEvent
    | LLMExchangeEvent
    | WarningEvent
    | ErrorEvent
    | DispatchesPlannedEvent
    | DispatchExecutedEvent,
    Field(discriminator="event"),
]
