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
    schema["required"] = list(schema.get("properties", {}).keys())


class PageType(str, Enum):
    SOURCE = "source"
    CLAIM = "claim"
    QUESTION = "question"
    JUDGEMENT = "judgement"
    WIKI = "wiki"
    SUMMARY = "summary"
    VIEW = "view"
    VIEW_ITEM = "view_item"
    VIEW_META = "view_meta"


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


class CallType(str, Enum):
    FIND_CONSIDERATIONS = "find_considerations"
    ASSESS = "assess"
    PRIORITIZATION = "prioritization"
    INGEST = "ingest"
    REFRAME = "reframe"
    MAINTAIN = "maintain"
    SUMMARIZE = "summarize"
    SCOUT_SUBQUESTIONS = "scout_subquestions"
    SCOUT_ESTIMATES = "scout_estimates"
    SCOUT_HYPOTHESES = "scout_hypotheses"
    SCOUT_ANALOGIES = "scout_analogies"
    SCOUT_PARADIGM_CASES = "scout_paradigm_cases"
    SCOUT_FACTCHECKS = "scout_factchecks"
    SCOUT_WEB_QUESTIONS = "scout_web_questions"
    SCOUT_DEEP_QUESTIONS = "scout_deep_questions"
    SCOUT_C_HOW_TRUE = "scout_c_how_true"
    SCOUT_C_HOW_FALSE = "scout_c_how_false"
    SCOUT_C_CRUXES = "scout_c_cruxes"
    SCOUT_C_RELEVANT_EVIDENCE = "scout_c_relevant_evidence"
    SCOUT_C_STRESS_TEST_CASES = "scout_c_stress_test_cases"
    SCOUT_C_ROBUSTIFY = "scout_c_robustify"
    SCOUT_C_STRENGTHEN = "scout_c_strengthen"
    WEB_RESEARCH = "web_research"
    EVALUATE = "evaluate"
    GROUNDING_FEEDBACK = "grounding_feedback"
    FEEDBACK_UPDATE = "feedback_update"
    LINK_SUBQUESTIONS = "link_subquestions"
    CREATE_VIEW = "create_view"


# The subset of CallTypes that prioritization can dispatch.
DISPATCHABLE_CALL_TYPES: set[CallType] = {
    CallType.FIND_CONSIDERATIONS,
    CallType.ASSESS,
    CallType.SCOUT_SUBQUESTIONS,
    CallType.SCOUT_ESTIMATES,
    CallType.SCOUT_HYPOTHESES,
    CallType.SCOUT_ANALOGIES,
    CallType.SCOUT_PARADIGM_CASES,
    CallType.SCOUT_FACTCHECKS,
    CallType.SCOUT_WEB_QUESTIONS,
    CallType.SCOUT_DEEP_QUESTIONS,
    CallType.SCOUT_C_HOW_TRUE,
    CallType.SCOUT_C_HOW_FALSE,
    CallType.SCOUT_C_CRUXES,
    CallType.SCOUT_C_RELEVANT_EVIDENCE,
    CallType.SCOUT_C_STRESS_TEST_CASES,
    CallType.SCOUT_C_ROBUSTIFY,
    CallType.SCOUT_C_STRENGTHEN,
    CallType.WEB_RESEARCH,
}


class CallStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"


class LinkType(str, Enum):
    CONSIDERATION = "consideration"  # claim -> question: claim should be accounted for in analysis of the question
    CHILD_QUESTION = "child_question"  # question decomposes into sub-question
    SUPERSEDES = "supersedes"  # page replaces another
    RELATED = "related"  # general relation
    ANSWERS = "answers"  # judgement -> question: this judgement is the current answer to the question
    VARIANT = "variant"  # more robust variation of a claim
    SUMMARIZES = "summarizes"  # summary page covers a question subtree
    CITES = "cites"  # claim cites a source
    DEPENDS_ON = "depends_on"  # claim/judgement -> claim/judgement: source page's conclusions rest on target being true/valid
    VIEW_ITEM = "view_item"  # view -> view_item: item belongs to this view
    VIEW_OF = "view_of"  # view -> question: this view covers this question
    META_FOR = "meta_for"  # view_meta -> view_item or view: meta annotation


class MoveType(str, Enum):
    CREATE_CLAIM = "CREATE_CLAIM"
    CREATE_QUESTION = "CREATE_QUESTION"
    CREATE_SCOUT_QUESTION = "CREATE_SCOUT_QUESTION"
    CREATE_SUBQUESTION = "CREATE_SUBQUESTION"
    CREATE_JUDGEMENT = "CREATE_JUDGEMENT"
    CREATE_WIKI_PAGE = "CREATE_WIKI_PAGE"
    LINK_CONSIDERATION = "LINK_CONSIDERATION"
    LINK_CHILD_QUESTION = "LINK_CHILD_QUESTION"
    LINK_RELATED = "LINK_RELATED"
    LINK_VARIANT = "LINK_VARIANT"
    FLAG_FUNNINESS = "FLAG_FUNNINESS"
    REPORT_DUPLICATE = "REPORT_DUPLICATE"
    LOAD_PAGE = "LOAD_PAGE"
    REMOVE_LINK = "REMOVE_LINK"
    CHANGE_LINK_ROLE = "CHANGE_LINK_ROLE"
    UPDATE_EPISTEMIC = "UPDATE_EPISTEMIC"
    LINK_DEPENDS_ON = "LINK_DEPENDS_ON"
    CREATE_VIEW_ITEM = "CREATE_VIEW_ITEM"
    PROPOSE_VIEW_ITEM = "PROPOSE_VIEW_ITEM"


class CallStage(str, Enum):
    BUILD_CONTEXT = "build_context"
    UPDATE_WORKSPACE = "update_workspace"
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
    reason: str = Field(
        default="", description="Why this dispatch is a good use of budget"
    )
    context_page_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Optional full UUIDs of pages to pre-load into the dispatched call. "
            "Use full UUIDs, not short IDs."
        ),
    )


class BaseDispatchPayload(_DispatchBase):
    question_id: str = Field(description="Page ID of the question to investigate")


class MultiRoundFields(BaseModel):
    fruit_threshold: int = Field(
        default=4, description="Remaining fruit threshold for stopping"
    )
    max_rounds: int = Field(
        default=5, description="Maximum scouting rounds (each round costs 1 budget)"
    )


class _ScoutFields(MultiRoundFields):
    mode: FindConsiderationsMode = Field(
        description=(
            "Scout mode: 'alternate' alternates abstract and concrete "
            "each round; 'abstract' for all-abstract; 'concrete' for all-concrete."
        ),
    )


class PrioritizationFields(BaseModel):
    budget: int = Field(description="Budget to allocate for the sub-investigation")


class ScoutDispatchPayload(BaseDispatchPayload, _ScoutFields):
    pass


class AssessDispatchPayload(BaseDispatchPayload):
    pass


def _hide_question_id(schema: dict) -> None:  # type: ignore[type-arg]
    props = schema.get("properties", {})
    props.pop("question_id", None)
    req = schema.get("required", [])
    if "question_id" in req:
        req.remove("question_id")


class ScopeOnlyDispatchPayload(BaseDispatchPayload):
    """Dispatch payload where question_id is injected at runtime, not by the LLM.

    The generated JSON schema hides question_id so the LLM tool never
    exposes it.  At bind time the orchestrator injects the scope question ID.
    """

    model_config = ConfigDict(json_schema_extra=_hide_question_id)
    question_id: str = Field(default="", description="Injected at runtime")


class ScoutSubquestionsDispatchPayload(ScopeOnlyDispatchPayload, MultiRoundFields):
    pass


class ScoutEstimatesDispatchPayload(ScopeOnlyDispatchPayload, MultiRoundFields):
    pass


class ScoutHypothesesDispatchPayload(ScopeOnlyDispatchPayload, MultiRoundFields):
    pass


class ScoutAnalogiesDispatchPayload(ScopeOnlyDispatchPayload, MultiRoundFields):
    pass


class ScoutParadigmCasesDispatchPayload(ScopeOnlyDispatchPayload, MultiRoundFields):
    pass


class ScoutFactchecksDispatchPayload(ScopeOnlyDispatchPayload, MultiRoundFields):
    pass


class ScoutWebQuestionsDispatchPayload(ScopeOnlyDispatchPayload, MultiRoundFields):
    pass


class ScoutDeepQuestionsDispatchPayload(ScopeOnlyDispatchPayload, MultiRoundFields):
    pass


class ScoutCHowTrueDispatchPayload(ScopeOnlyDispatchPayload, MultiRoundFields):
    pass


class ScoutCHowFalseDispatchPayload(ScopeOnlyDispatchPayload, MultiRoundFields):
    pass


class ScoutCCruxesDispatchPayload(ScopeOnlyDispatchPayload, MultiRoundFields):
    pass


class ScoutCRelevantEvidenceDispatchPayload(ScopeOnlyDispatchPayload, MultiRoundFields):
    pass


class ScoutCStressTestCasesDispatchPayload(ScopeOnlyDispatchPayload, MultiRoundFields):
    pass


class ScoutCRobustifyDispatchPayload(ScopeOnlyDispatchPayload, MultiRoundFields):
    pass


class ScoutCStrengthenDispatchPayload(ScopeOnlyDispatchPayload, MultiRoundFields):
    pass


class CreateViewDispatchPayload(BaseDispatchPayload):
    pass


class WebResearchDispatchPayload(BaseDispatchPayload):
    pass


class RecurseDispatchPayload(BaseDispatchPayload, PrioritizationFields):
    budget: int = Field(
        ge=MIN_TWOPHASE_BUDGET,
        description=f"Budget to allocate for the sub-investigation (minimum {MIN_TWOPHASE_BUDGET})",
    )


class RecurseClaimDispatchPayload(BaseDispatchPayload, PrioritizationFields):
    budget: int = Field(
        ge=MIN_TWOPHASE_BUDGET,
        description=f"Budget for the claim sub-investigation (minimum {MIN_TWOPHASE_BUDGET})",
    )


class InlineScoutDispatch(_DispatchBase, _ScoutFields):
    call_type: Literal["find_considerations"] = "find_considerations"


class InlineAssessDispatch(_DispatchBase):
    call_type: Literal["assess"] = "assess"


InlineDispatch = Annotated[
    InlineScoutDispatch | InlineAssessDispatch,
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
    hidden: bool = False


class Page(BaseModel):
    model_config = ConfigDict(json_schema_extra=_all_fields_required)
    page_type: PageType
    layer: PageLayer
    workspace: Workspace
    content: str
    headline: str
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id: str = ""
    epistemic_status: float = 2.5  # DEPRECATED — kept for backward compat
    epistemic_type: str = ""  # DEPRECATED — kept for backward compat
    credence: int | None = None  # 1-9 probability bucket (claims/judgements only)
    robustness: int | None = None  # 1-5 resilience of view (claims/judgements only)
    provenance_model: str = ""
    provenance_call_type: str = ""
    provenance_call_id: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    superseded_by: str | None = None
    is_superseded: bool = False
    extra: dict = Field(default_factory=dict)
    abstract: str = ""
    fruit_remaining: int | None = None
    sections: list[str] | None = None  # VIEW pages: ordered section names
    meta_type: str | None = None  # VIEW_META pages: priority/annotation/proposal

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
    importance: int | None = None  # VIEW_ITEM links: 1-5
    section: str | None = None  # VIEW_ITEM links: section name
    position: int | None = None  # VIEW_ITEM links: order within section
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
    cost_usd: float | None = None
