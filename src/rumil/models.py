"""
Data models for the research workspace.
"""

from datetime import UTC, datetime
from enum import Enum
import uuid

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Discriminator, Field


def _all_fields_required(schema: dict) -> None:
    """Mark all fields as required in JSON schema.

    Models used as API response types need this because fields with defaults
    (like ``id`` or ``created_at``) are always populated in responses, but
    Pydantic marks them optional in the schema by default.
    """
    schema['required'] = list(schema.get('properties', {}).keys())


class PageType(str, Enum):
    SOURCE = "source"
    CLAIM = "claim"
    QUESTION = "question"
    JUDGEMENT = "judgement"
    CONCEPT = "concept"
    WIKI = "wiki"


class PageLayer(str, Enum):
    WIKI = "wiki"
    SQUIDGY = "squidgy"


class Workspace(str, Enum):
    RESEARCH = "research"
    PRIORITIZATION = "prioritization"


class CallType(str, Enum):
    SCOUT = "scout"
    ASSESS = "assess"
    PRIORITIZATION = "prioritization"
    INGEST = "ingest"
    REFRAME = "reframe"
    MAINTAIN = "maintain"


# The subset of CallTypes that prioritization can dispatch.
DISPATCHABLE_CALL_TYPES: set[CallType] = {
    CallType.SCOUT,
    CallType.ASSESS,
    CallType.PRIORITIZATION,
}


class CallStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"


class LinkType(str, Enum):
    CONSIDERATION = "consideration"  # claim bears on a question
    CHILD_QUESTION = "child_question"  # question decomposes into sub-question
    SUPERSEDES = "supersedes"  # page replaces another
    RELATED = "related"  # general relation


class MoveType(str, Enum):
    CREATE_CLAIM = "CREATE_CLAIM"
    CREATE_QUESTION = "CREATE_QUESTION"
    CREATE_JUDGEMENT = "CREATE_JUDGEMENT"
    CREATE_CONCEPT = "CREATE_CONCEPT"
    CREATE_WIKI_PAGE = "CREATE_WIKI_PAGE"
    LINK_CONSIDERATION = "LINK_CONSIDERATION"
    LINK_CHILD_QUESTION = "LINK_CHILD_QUESTION"
    LINK_RELATED = "LINK_RELATED"
    SUPERSEDE_PAGE = "SUPERSEDE_PAGE"
    FLAG_FUNNINESS = "FLAG_FUNNINESS"
    REPORT_DUPLICATE = "REPORT_DUPLICATE"
    PROPOSE_HYPOTHESIS = "PROPOSE_HYPOTHESIS"
    LOAD_PAGE = "LOAD_PAGE"


class ScoutMode(str, Enum):
    ALTERNATE = "alternate"
    ABSTRACT = "abstract"
    CONCRETE = "concrete"


class ConsiderationDirection(str, Enum):
    SUPPORTS = "supports"
    OPPOSES = "opposes"
    NEUTRAL = "neutral"


class _DispatchBase(BaseModel):
    reason: str = Field("", description="Why this dispatch is a good use of budget")
    context_page_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Optional full UUIDs of pages to pre-load into the dispatched call. "
            "Use full UUIDs, not short IDs."
        ),
    )


class BaseDispatchPayload(_DispatchBase):
    question_id: str = Field(description="Page ID of the question to investigate")


class _ScoutFields(BaseModel):
    mode: ScoutMode = Field(
        ScoutMode.ALTERNATE,
        description=(
            "Scout mode: 'alternate' (default) alternates abstract and concrete "
            "each round; 'abstract' for all-abstract; 'concrete' for all-concrete."
        ),
    )
    fruit_threshold: int = Field(
        4, description="Remaining fruit threshold for stopping"
    )
    max_rounds: int = Field(
        5, description="Maximum scouting rounds (each round costs 1 budget)"
    )


class _PrioritizationFields(BaseModel):
    budget: int = Field(description="Budget to allocate for the sub-investigation")


class ScoutDispatchPayload(BaseDispatchPayload, _ScoutFields):
    pass


class AssessDispatchPayload(BaseDispatchPayload):
    pass


class PrioritizationDispatchPayload(BaseDispatchPayload, _PrioritizationFields):
    pass


class InlineScoutDispatch(_DispatchBase, _ScoutFields):
    call_type: Literal["scout"] = "scout"


class InlineAssessDispatch(_DispatchBase):
    call_type: Literal["assess"] = "assess"


class InlinePrioritizationDispatch(_DispatchBase, _PrioritizationFields):
    call_type: Literal["prioritization"] = "prioritization"


InlineDispatch = Annotated[
    InlineScoutDispatch | InlineAssessDispatch | InlinePrioritizationDispatch,
    Discriminator("call_type"),
]


class Move(BaseModel):
    move_type: MoveType
    payload: BaseModel


class Dispatch(BaseModel):
    call_type: CallType
    payload: BaseDispatchPayload


class Project(BaseModel):
    model_config = ConfigDict(json_schema_extra=_all_fields_required)
    name: str
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Page(BaseModel):
    model_config = ConfigDict(json_schema_extra=_all_fields_required)
    page_type: PageType
    layer: PageLayer
    workspace: Workspace
    content: str
    summary: str
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id: str = ""
    epistemic_status: float = 2.5  # 0-5 subjective confidence
    epistemic_type: str = ""  # description of uncertainty type
    provenance_model: str = ""
    provenance_call_type: str = ""
    provenance_call_id: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    superseded_by: str | None = None
    is_superseded: bool = False
    extra: dict = Field(default_factory=dict)

    def is_active(self) -> bool:
        return not self.is_superseded


class PageLink(BaseModel):
    model_config = ConfigDict(json_schema_extra=_all_fields_required)
    from_page_id: str
    to_page_id: str
    link_type: LinkType
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    direction: ConsiderationDirection | None = None  # for CONSIDERATION links
    strength: float = 2.5  # 0-5
    reasoning: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Call(BaseModel):
    model_config = ConfigDict(json_schema_extra=_all_fields_required)
    call_type: CallType
    workspace: Workspace
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id: str = ""
    status: CallStatus = CallStatus.PENDING
    parent_call_id: str | None = None
    scope_page_id: str | None = None  # question/consideration this call is about
    budget_allocated: int | None = None
    budget_used: int = 0
    context_page_ids: list[str] = Field(default_factory=list)
    result_summary: str = ""
    review_json: dict = Field(default_factory=dict)
    call_params: dict | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None
