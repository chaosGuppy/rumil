"""
Data models for the research workspace.
"""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional
import uuid


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
    CONSIDERATION = "consideration"       # claim bears on a question
    CHILD_QUESTION = "child_question"     # question decomposes into sub-question
    SUPERSEDES = "supersedes"             # page replaces another
    RELATED = "related"                   # general relation


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


class ConsiderationDirection(str, Enum):
    SUPPORTS = "supports"
    OPPOSES = "opposes"
    NEUTRAL = "neutral"


@dataclass
class Page:
    page_type: PageType
    layer: PageLayer
    workspace: Workspace
    content: str
    summary: str
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    epistemic_status: float = 2.5          # 0-5 subjective confidence
    epistemic_type: str = ""               # description of uncertainty type
    provenance_model: str = ""
    provenance_call_type: str = ""
    provenance_call_id: str = ""
    created_at: datetime = field(default_factory=datetime.utcnow)
    superseded_by: Optional[str] = None
    is_superseded: bool = False
    extra: dict = field(default_factory=dict)

    def is_active(self) -> bool:
        return not self.is_superseded


@dataclass
class PageLink:
    from_page_id: str
    to_page_id: str
    link_type: LinkType
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    direction: Optional[ConsiderationDirection] = None  # for CONSIDERATION links
    strength: float = 2.5                               # 0-5
    reasoning: str = ""
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class Call:
    call_type: CallType
    workspace: Workspace
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: CallStatus = CallStatus.PENDING
    parent_call_id: Optional[str] = None
    scope_page_id: Optional[str] = None   # question/consideration this call is about
    budget_allocated: Optional[int] = None
    budget_used: int = 0
    context_page_ids: list = field(default_factory=list)
    result_summary: str = ""
    review_json: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None
