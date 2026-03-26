"""Typed trace event models for the execution tracer."""

from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, Field, model_validator


class PageRef(BaseModel):
    id: str
    headline: str = ""

    @model_validator(mode="before")
    @classmethod
    def _migrate_summary(cls, values: dict) -> dict:
        if (
            isinstance(values, dict)
            and "summary" in values
            and "headline" not in values
        ):
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
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    duration_ms: int | None = None
    cost_usd: float | None = None


class WarningEvent(BaseModel):
    event: Literal["warning"] = "warning"
    message: str


class ErrorEvent(BaseModel):
    event: Literal["error"] = "error"
    message: str
    phase: str = ""


class SubquestionScoreItem(BaseModel):
    question_id: str
    headline: str = ""
    impact: int = 0
    fruit: int = 0
    reasoning: str = ""


class ScoringCompletedEvent(BaseModel):
    event: Literal["scoring_completed"] = "scoring_completed"
    subquestion_scores: list[SubquestionScoreItem] = []
    parent_fruit: int | None = None
    parent_fruit_reasoning: str = ""


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


class AgentStartedEvent(BaseModel):
    event: Literal["agent_started"] = "agent_started"
    system_prompt: str = ""
    user_message: str = ""


class EvaluationCompleteEvent(BaseModel):
    event: Literal["evaluation_complete"] = "evaluation_complete"
    evaluation: str = ""


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
    | EvaluationCompleteEvent,
    Field(discriminator="event"),
]
