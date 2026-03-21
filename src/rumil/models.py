"""
Data models for the research workspace.
"""

from datetime import UTC, datetime
from enum import Enum
import uuid

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Discriminator, Field

from rumil.constants import MIN_TWOPHASE_BUDGET


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
    SUMMARY = "summary"


class PageDetail(str, Enum):
    HEADLINE = "headline"
    ABSTRACT = "abstract"
    CONTENT = "content"


class PageLayer(str, Enum):
    WIKI = "wiki"
    SQUIDGY = "squidgy"


class Workspace(str, Enum):
    RESEARCH = "research"
    PRIORITIZATION = "prioritization"
    CONCEPT_STAGING = "concept_staging"


class CallType(str, Enum):
    FIND_CONSIDERATIONS = "find_considerations"
    ASSESS = "assess"
    PRIORITIZATION = "prioritization"
    INGEST = "ingest"
    REFRAME = "reframe"
    MAINTAIN = "maintain"
    SUMMARIZE = "summarize"
    SCOUT_CONCEPTS = "scout_concepts"
    ASSESS_CONCEPT = "assess_concept"
    SCOUT_SUBQUESTIONS = "scout_subquestions"
    SCOUT_ESTIMATES = "scout_estimates"
    SCOUT_HYPOTHESES = "scout_hypotheses"
    SCOUT_ANALOGIES = "scout_analogies"
    SCOUT_PARADIGM_CASES = "scout_paradigm_cases"
    SCOUT_FACTS_TO_CHECK = "scout_facts_to_check"
    WEB_RESEARCH = "web_research"


# The subset of CallTypes that prioritization can dispatch.
DISPATCHABLE_CALL_TYPES: set[CallType] = {
    CallType.FIND_CONSIDERATIONS,
    CallType.ASSESS,
    CallType.PRIORITIZATION,
    CallType.SCOUT_SUBQUESTIONS,
    CallType.SCOUT_ESTIMATES,
    CallType.SCOUT_HYPOTHESES,
    CallType.SCOUT_ANALOGIES,
    CallType.SCOUT_PARADIGM_CASES,
    CallType.SCOUT_FACTS_TO_CHECK,
    CallType.WEB_RESEARCH,
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
    SUMMARIZES = "summarizes"  # summary page covers a question subtree
    CITES = "cites"  # claim cites a source


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
    REMOVE_LINK = "REMOVE_LINK"
    CHANGE_LINK_ROLE = "CHANGE_LINK_ROLE"
    PROPOSE_CONCEPT = "PROPOSE_CONCEPT"
    PROMOTE_CONCEPT = "PROMOTE_CONCEPT"


class CallStage(str, Enum):
    BUILD_CONTEXT = "build_context"
    CREATE_PAGES = "create_pages"
    CLOSING_REVIEW = "closing_review"


class FindConsiderationsMode(str, Enum):
    ALTERNATE = "alternate"
    ABSTRACT = "abstract"
    CONCRETE = "concrete"


class LinkRole(str, Enum):
    DIRECT = "direct"
    STRUCTURAL = "structural"


class ConsiderationDirection(str, Enum):
    SUPPORTS = "supports"
    OPPOSES = "opposes"
    NEUTRAL = "neutral"


class _DispatchBase(BaseModel):
    reason: str = Field(default="", description="Why this dispatch is a good use of budget")
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
    mode: FindConsiderationsMode = Field(
        default=FindConsiderationsMode.ALTERNATE,
        description=(
            "Scout mode: 'alternate' (default) alternates abstract and concrete "
            "each round; 'abstract' for all-abstract; 'concrete' for all-concrete."
        ),
    )
    fruit_threshold: int = Field(
        default=4, description="Remaining fruit threshold for stopping"
    )
    max_rounds: int = Field(
        default=5, description="Maximum scouting rounds (each round costs 1 budget)"
    )


class _PrioritizationFields(BaseModel):
    budget: int = Field(description="Budget to allocate for the sub-investigation")


class ScoutDispatchPayload(BaseDispatchPayload, _ScoutFields):
    pass


class AssessDispatchPayload(BaseDispatchPayload):
    pass


class PrioritizationDispatchPayload(BaseDispatchPayload, _PrioritizationFields):
    pass


class ScoutSubquestionsDispatchPayload(BaseDispatchPayload):
    pass


class ScoutEstimatesDispatchPayload(BaseDispatchPayload):
    pass


class ScoutHypothesesDispatchPayload(BaseDispatchPayload):
    pass


class ScoutAnalogiesDispatchPayload(BaseDispatchPayload):
    pass


class ScoutParadigmCasesDispatchPayload(BaseDispatchPayload):
    pass


class ScoutFactsToCheckDispatchPayload(BaseDispatchPayload):
    pass


class WebResearchDispatchPayload(BaseDispatchPayload):
    pass


class RecurseDispatchPayload(BaseDispatchPayload, _PrioritizationFields):
    budget: int = Field(
        ge=MIN_TWOPHASE_BUDGET,
        description=f'Budget to allocate for the sub-investigation (minimum {MIN_TWOPHASE_BUDGET})',
    )


class InlineScoutDispatch(_DispatchBase, _ScoutFields):
    call_type: Literal["find_considerations"] = "find_considerations"


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
    headline: str
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
    abstract: str = ""

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
    role: LinkRole = LinkRole.DIRECT
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class CallSequence(BaseModel):
    model_config = ConfigDict(json_schema_extra=_all_fields_required)
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    parent_call_id: str | None = None
    run_id: str = ""
    scope_question_id: str | None = None
    position_in_batch: int = 0
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
    sequence_id: str | None = None
    sequence_position: int | None = None
