"""
Data models for the research workspace.
"""

import uuid
from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

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
    VIEW = "view"
    VIEW_ITEM = "view_item"
    VIEW_META = "view_meta"
    ARTIFACT = "artifact"
    MODEL = "model"
    # Model-authored UI fragment: an HTML/CSS/JS blob rendered in a
    # sandboxed iframe as a custom content-area renderer for a question.
    # See planning/inlay-ui.md. Phase 1 (MVP) is hand-authored via
    # scripts/create_inlay.py; phase 2 is chat authoring; phase 3 is
    # orchestrator dispatch (AuthorInlayCall).
    INLAY = "inlay"


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
    AB_EVAL = "ab_eval"
    AB_EVAL_COMPARISON = "ab_eval_comparison"
    AB_EVAL_SUMMARY = "ab_eval_summary"
    RUN_EVAL = "run_eval"
    SINGLE_CALL_BASELINE = "single_call_baseline"
    CREATE_VIEW = "create_view"
    GLOBAL_PRIORITIZATION = "global_prioritization"
    UPDATE_VIEW = "update_view"
    CHAT_DIRECT = "chat_direct"
    ADVERSARIAL_REVIEW = "adversarial_review"
    EXPLORE_TENSION = "explore_tension"
    DRAFT_ARTIFACT = "draft_artifact"
    BUILD_MODEL = "build_model"
    # Envelope call for mutations made from Claude Code's broader context
    # (not a rumil-internal call with carefully scoped prompt). Never
    # dispatchable from prioritization — only created by .claude/ skills.
    CLAUDE_CODE_DIRECT = "claude_code_direct"
    # Reserved for Phase 3. Orchestrator-dispatched call that generates an
    # Inlay page (model-authored HTML/CSS/JS for a question's custom view).
    # Not implemented in the MVP — the enum is reserved here so data
    # written by phase 1 (hand-authored) and phase 2 (chat) can round-trip
    # through the same CallType namespace without future migrations.
    AUTHOR_INLAY = "author_inlay"


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
    CallType.BUILD_MODEL,
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
    ANSWERS = (
        "answers"  # judgement -> question: this judgement is the current answer to the question
    )
    VARIANT = "variant"  # more robust variation of a claim
    SUMMARIZES = "summarizes"  # summary page covers a question subtree
    CITES = "cites"  # claim cites a source
    DEPENDS_ON = "depends_on"  # claim/judgement -> claim/judgement: source page's conclusions rest on target being true/valid
    VIEW_ITEM = "view_item"  # view -> view_item: item belongs to this view
    VIEW_OF = "view_of"  # view -> question: this view covers this question
    META_FOR = "meta_for"  # view_meta -> view_item or view: meta annotation
    MODEL_OF = (
        "model_of"  # model -> question: this model captures structure bearing on this question
    )
    INLAY_OF = "inlay_of"  # inlay -> question (or project): inlay renders this target


class MoveType(str, Enum):
    CREATE_CLAIM = "CREATE_CLAIM"
    CREATE_QUESTION = "CREATE_QUESTION"
    CREATE_SCOUT_QUESTION = "CREATE_SCOUT_QUESTION"
    CREATE_JUDGEMENT = "CREATE_JUDGEMENT"
    CREATE_WIKI_PAGE = "CREATE_WIKI_PAGE"
    LINK_CONSIDERATION = "LINK_CONSIDERATION"
    LINK_CHILD_QUESTION = "LINK_CHILD_QUESTION"
    LINK_RELATED = "LINK_RELATED"
    LINK_VARIANT = "LINK_VARIANT"
    FLAG_FUNNINESS = "FLAG_FUNNINESS"
    FLAG_ISSUE = "FLAG_ISSUE"
    REPORT_DUPLICATE = "REPORT_DUPLICATE"
    LOAD_PAGE = "LOAD_PAGE"
    REMOVE_LINK = "REMOVE_LINK"
    CHANGE_LINK_ROLE = "CHANGE_LINK_ROLE"
    UPDATE_EPISTEMIC = "UPDATE_EPISTEMIC"
    CREATE_VIEW_ITEM = "CREATE_VIEW_ITEM"
    PROPOSE_VIEW_ITEM = "PROPOSE_VIEW_ITEM"
    ANNOTATE_SPAN = "ANNOTATE_SPAN"
    ANNOTATE_ALTERNATIVE = "ANNOTATE_ALTERNATIVE"
    WRITE_MODEL_BODY = "WRITE_MODEL_BODY"


class ModelFlavor(str, Enum):
    """Flavor of a model-building call.

    `theoretical` — no code execution; the LLM writes a structured
    theoretical model (variables, relations, predictions, assumptions).
    The `executable` flavor is deferred pending a separate sandboxing
    design doc and will be added as a new enum value here when ready.
    """

    THEORETICAL = "theoretical"


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


class MultiRoundFields(BaseModel):
    fruit_threshold: int = Field(default=4, description="Remaining fruit threshold for stopping")
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


class UpdateViewDispatchPayload(BaseDispatchPayload):
    pass


class WebResearchDispatchPayload(BaseDispatchPayload):
    pass


class BuildModelDispatchPayload(BaseDispatchPayload):
    flavor: ModelFlavor = Field(
        default=ModelFlavor.THEORETICAL,
        description=(
            "Model flavor. Only 'theoretical' is supported today; the "
            "executable flavor is deferred pending a separate sandboxing "
            "design doc."
        ),
    )


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
    credence: int | None = None  # 1-9 probability bucket (claim pages only)
    credence_reasoning: str | None = None
    robustness: int | None = None  # 1-5 resilience of view (any non-question page)
    robustness_reasoning: str | None = None
    provenance_model: str = ""
    provenance_call_type: str = ""
    provenance_call_id: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    superseded_by: str | None = None
    is_superseded: bool = False
    extra: dict = Field(default_factory=dict)
    importance: int | None = None  # 0-4 editorial importance (0=core, 4=deep supplementary)
    abstract: str = ""
    fruit_remaining: int | None = None
    sections: list[str] | None = None  # VIEW pages: ordered section names
    meta_type: str | None = None  # VIEW_META pages: priority/annotation/proposal
    run_id: str = ""
    task_shape: dict | None = None  # v1 task-shape taxonomy, questions only

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
    impact_on_parent_question: int | None = None  # CHILD_QUESTION links: 0-10
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    run_id: str = ""


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


class SuggestionType(str, Enum):
    CASCADE_REVIEW = "cascade_review"
    RELEVEL = "relevel"
    RESOLVE_TENSION = "resolve_tension"
    MERGE_DUPLICATE = "merge_duplicate"
    AUTO_INVESTIGATE = "auto_investigate"


class SuggestionStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    DISMISSED = "dismissed"


class Suggestion(BaseModel):
    model_config = ConfigDict(json_schema_extra=_all_fields_required)
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id: str = ""
    workspace: str = "research"
    run_id: str = ""
    suggestion_type: SuggestionType
    target_page_id: str
    source_page_id: str | None = None
    payload: dict = Field(default_factory=dict)
    status: SuggestionStatus = SuggestionStatus.PENDING
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    reviewed_at: datetime | None = None
    staged: bool = False


class ReputationEvent(BaseModel):
    """One append-only reputation signal.

    The table is a multi-source substrate: each event is tagged with its
    ``source`` (eval_agent, human_feedback, proposal_survival, budget_flow,
    subscription) and ``dimension`` (consistency, general_quality, grounding,
    ...). Consumers aggregate at query time with their own weighting scheme.
    Never collapse to a scalar at write time.
    """

    model_config = ConfigDict(json_schema_extra=_all_fields_required)
    run_id: str
    project_id: str
    source: str
    dimension: str
    score: float
    orchestrator: str | None = None
    task_shape: dict | None = None
    source_call_id: str | None = None
    extra: dict = Field(default_factory=dict)
    staged: bool = False
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AnnotationEvent(BaseModel):
    """One append-only annotation signal.

    The annotation substrate is one events table for human- and model-authored
    feedback on pages, spans, calls, and specific trace events. Never collapse
    at write time; consumers aggregate at query time. See
    marketplace-thread/28-annotation-primitives.md.

    ``annotation_type`` is a string rather than an enum so new kinds can be
    added without a migration. The MVP uses: ``span``, ``counterfactual``,
    ``flag``, ``endorsement``. ``category`` is a coarse bucket (e.g.
    ``factual_error``, ``missing_consideration``, ``tool_choice``) set
    per-annotation; ``payload`` carries annotation-type-specific extras.
    """

    model_config = ConfigDict(json_schema_extra=_all_fields_required)
    annotation_type: str
    author_type: str
    author_id: str
    target_page_id: str | None = None
    target_call_id: str | None = None
    target_event_seq: int | None = None
    span_start: int | None = None
    span_end: int | None = None
    category: str | None = None
    note: str = ""
    payload: dict = Field(default_factory=dict)
    extra: dict = Field(default_factory=dict)
    run_id: str | None = None
    project_id: str | None = None
    staged: bool = False
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class NudgeKind(str, Enum):
    CONSTRAIN_DISPATCH = "constrain_dispatch"
    INJECT_NOTE = "inject_note"
    REWRITE_GOAL = "rewrite_goal"
    VETO_CALL = "veto_call"
    REDO_CALL = "redo_call"
    PAUSE = "pause"


class NudgeDurability(str, Enum):
    ONE_SHOT = "one_shot"
    PERSISTENT = "persistent"


class NudgeAuthorKind(str, Enum):
    HUMAN = "human"
    CLAUDE = "claude"
    SYSTEM = "system"


class NudgeStatus(str, Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"
    CONSUMED = "consumed"


class NudgeScope(BaseModel):
    """Where a nudge applies. All fields optional; empty scope matches
    everything on the run. Stored as JSONB on ``run_nudges.scope``."""

    model_config = ConfigDict(json_schema_extra=_all_fields_required)
    call_types: list[str] | None = None
    question_ids: list[str] | None = None
    call_id: str | None = None
    expires_at: datetime | None = None
    expires_after_n_calls: int | None = None


class RunNudge(BaseModel):
    model_config = ConfigDict(json_schema_extra=_all_fields_required)
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    run_id: str
    author_kind: NudgeAuthorKind
    author_note: str = ""
    kind: NudgeKind
    payload: dict = Field(default_factory=dict)
    durability: NudgeDurability
    scope: NudgeScope = Field(default_factory=NudgeScope)
    soft_text: str | None = None
    hard: bool = False
    status: NudgeStatus = NudgeStatus.ACTIVE
    staged: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    revoked_at: datetime | None = None
    consumed_at: datetime | None = None


class AlertKind(str, Enum):
    COST_THRESHOLD = "cost_threshold"
    STALL_TIMEOUT = "stall_timeout"
    CONFUSION_SPIKE = "confusion_spike"


class AlertSeverity(str, Enum):
    INFO = "info"
    WARN = "warn"
    CRIT = "crit"


class AlertConfig(BaseModel):
    """A rule that turns run state into fired alerts at read / tick time."""

    model_config = ConfigDict(json_schema_extra=_all_fields_required)
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id: str | None = None
    run_id: str | None = None
    kind: AlertKind
    params: dict = Field(default_factory=dict)
    enabled: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class FiredAlert(BaseModel):
    """In-memory alert fired for a specific run on evaluator read.

    Not persisted in v1 — consumers (parma dashboard, orchestrator tick
    emitter) receive the list and decide what to display or forward.
    """

    model_config = ConfigDict(json_schema_extra=_all_fields_required)
    run_id: str
    kind: AlertKind
    severity: AlertSeverity = AlertSeverity.INFO
    message: str
    context: dict = Field(default_factory=dict)
    source_config_id: str | None = None


class ChatMessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"
    SYSTEM = "system"


class ChatConversation(BaseModel):
    model_config = ConfigDict(json_schema_extra=_all_fields_required)
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id: str
    question_id: str | None = None
    title: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    deleted_at: datetime | None = None
    staged: bool = False
    run_id: str | None = None
    # Branching metadata. `parent_conversation_id` is the source conversation
    # this was branched from (null for original, non-branched conversations);
    # `branched_at_seq` is the highest `chat_messages.seq` value that was copied
    # over from the parent. See
    # supabase/migrations/20260419023712_chat_conversation_branch_fields.sql.
    parent_conversation_id: str | None = None
    branched_at_seq: int | None = None


class ChatMessage(BaseModel):
    model_config = ConfigDict(json_schema_extra=_all_fields_required)
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    conversation_id: str
    role: ChatMessageRole
    content: dict = Field(default_factory=dict)
    seq: int = 0
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    staged: bool = False
    run_id: str | None = None
    question_id: str | None = None
