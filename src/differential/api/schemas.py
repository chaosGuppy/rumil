"""
Pydantic schemas for the API layer.

These mirror the dataclass models but are proper Pydantic models
for FastAPI serialization/validation.
"""

from datetime import datetime

from pydantic import BaseModel

from differential.models import (
    CallStatus,
    CallType,
    ConsiderationDirection,
    LinkType,
    PageLayer,
    PageType,
    Workspace,
)


class ProjectOut(BaseModel):
    id: str
    name: str
    created_at: datetime


class PageOut(BaseModel):
    id: str
    page_type: PageType
    layer: PageLayer
    workspace: Workspace
    content: str
    summary: str
    project_id: str
    epistemic_status: float
    epistemic_type: str
    provenance_model: str
    provenance_call_type: str
    provenance_call_id: str
    created_at: datetime
    superseded_by: str | None
    is_superseded: bool
    extra: dict


class PageLinkOut(BaseModel):
    id: str
    from_page_id: str
    to_page_id: str
    link_type: LinkType
    direction: ConsiderationDirection | None
    strength: float
    reasoning: str
    created_at: datetime


class CallOut(BaseModel):
    id: str
    call_type: CallType
    workspace: Workspace
    project_id: str
    status: CallStatus
    parent_call_id: str | None
    scope_page_id: str | None
    budget_allocated: int | None
    budget_used: int
    context_page_ids: list[str]
    result_summary: str
    review_json: dict
    created_at: datetime
    completed_at: datetime | None


class ConsiderationOut(BaseModel):
    page: PageOut
    link: PageLinkOut


class QuestionTreeOut(BaseModel):
    question: PageOut
    considerations: list[ConsiderationOut]
    judgements: list[PageOut]
    child_questions: list['QuestionTreeOut']


class PageCountsOut(BaseModel):
    considerations: int
    judgements: int
