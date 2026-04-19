"""Row-to-model conversion helpers.

Pure functions that map raw Supabase rows (``dict[str, Any]``) to typed
Pydantic models. These are shared across stores and ``database.py`` and
have no knowledge of staged runs or mutation events.
"""

from datetime import datetime
from typing import Any, cast

from rumil.models import (
    AnnotationEvent,
    Call,
    CallSequence,
    CallStatus,
    CallType,
    ConsiderationDirection,
    LinkRole,
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Suggestion,
    SuggestionStatus,
    SuggestionType,
    Workspace,
)

_Rows = list[dict[str, Any]]


def _rows(response: Any) -> _Rows:
    """Extract rows from a Supabase API response with proper typing."""
    return cast(_Rows, response.data) if response.data else []


_LINK_COLUMNS = (
    "id,from_page_id,to_page_id,link_type,direction,"
    "strength,reasoning,role,importance,section,position,"
    "impact_on_parent_question,created_at,run_id"
)

_SLIM_PAGE_COLUMNS = (
    "id,page_type,layer,workspace,headline,abstract,"
    "epistemic_status,epistemic_type,credence,robustness,importance,extra,is_superseded,"
    "project_id,created_at,superseded_by,run_id"
)


def _row_to_page(row: dict[str, Any]) -> Page:
    return Page(
        id=row["id"],
        page_type=PageType(row["page_type"]),
        layer=PageLayer(row["layer"]),
        workspace=Workspace(row["workspace"]),
        content=row.get("content") or "",
        headline=row["headline"],
        project_id=row.get("project_id") or "",
        epistemic_status=row.get("epistemic_status") or 0.0,
        epistemic_type=row.get("epistemic_type") or "",
        credence=row.get("credence"),
        credence_reasoning=row.get("credence_reasoning"),
        robustness=row.get("robustness"),
        robustness_reasoning=row.get("robustness_reasoning"),
        importance=row.get("importance"),
        provenance_model=row.get("provenance_model") or "",
        provenance_call_type=row.get("provenance_call_type") or "",
        provenance_call_id=row.get("provenance_call_id") or "",
        created_at=datetime.fromisoformat(row["created_at"]),
        superseded_by=row.get("superseded_by"),
        is_superseded=bool(row.get("is_superseded", False)),
        extra=row.get("extra") or {},
        abstract=row.get("abstract") or "",
        fruit_remaining=row.get("fruit_remaining"),
        sections=row.get("sections"),
        meta_type=row.get("meta_type"),
        run_id=row.get("run_id") or "",
        task_shape=row.get("task_shape"),
    )


def _row_to_link(row: dict[str, Any]) -> PageLink:
    return PageLink(
        id=row["id"],
        from_page_id=row["from_page_id"],
        to_page_id=row["to_page_id"],
        link_type=LinkType(row["link_type"]),
        direction=(ConsiderationDirection(row["direction"]) if row["direction"] else None),
        strength=row["strength"],
        reasoning=row["reasoning"] or "",
        role=LinkRole(row.get("role", "direct")),
        importance=row.get("importance"),
        section=row.get("section"),
        position=row.get("position"),
        impact_on_parent_question=row.get("impact_on_parent_question"),
        created_at=datetime.fromisoformat(row["created_at"]),
        run_id=row.get("run_id") or "",
    )


def _row_to_call(row: dict[str, Any]) -> Call:
    return Call(
        id=row["id"],
        call_type=CallType(row["call_type"]),
        workspace=Workspace(row["workspace"]),
        project_id=row.get("project_id") or "",
        status=CallStatus(row["status"]),
        parent_call_id=row["parent_call_id"],
        scope_page_id=row["scope_page_id"],
        budget_allocated=row["budget_allocated"],
        budget_used=row["budget_used"],
        context_page_ids=row.get("context_page_ids") or [],
        result_summary=row.get("result_summary") or "",
        review_json=row.get("review_json") or {},
        call_params=row.get("call_params"),
        created_at=datetime.fromisoformat(row["created_at"]),
        completed_at=(datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None),
        sequence_id=row.get("sequence_id"),
        sequence_position=row.get("sequence_position"),
        cost_usd=row.get("cost_usd"),
    )


def _row_to_suggestion(row: dict[str, Any]) -> Suggestion:
    return Suggestion(
        id=str(row["id"]),
        project_id=row.get("project_id") or "",
        workspace=row.get("workspace") or "research",
        run_id=row.get("run_id") or "",
        suggestion_type=SuggestionType(row["suggestion_type"]),
        target_page_id=row["target_page_id"],
        source_page_id=row.get("source_page_id"),
        payload=row.get("payload") or {},
        status=SuggestionStatus(row["status"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        reviewed_at=(
            datetime.fromisoformat(row["reviewed_at"]) if row.get("reviewed_at") else None
        ),
        staged=bool(row.get("staged", False)),
    )


def _row_to_call_sequence(row: dict[str, Any]) -> CallSequence:
    return CallSequence(
        id=row["id"],
        parent_call_id=row.get("parent_call_id"),
        run_id=row.get("run_id", ""),
        scope_question_id=row.get("scope_question_id"),
        position_in_batch=row.get("position_in_batch", 0),
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _row_to_annotation_event(row: dict[str, Any]) -> AnnotationEvent:
    return AnnotationEvent(
        id=row["id"],
        project_id=row.get("project_id"),
        run_id=row.get("run_id"),
        annotation_type=row["annotation_type"],
        author_type=row["author_type"],
        author_id=row["author_id"],
        target_page_id=row.get("target_page_id"),
        target_call_id=row.get("target_call_id"),
        target_event_seq=row.get("target_event_seq"),
        span_start=row.get("span_start"),
        span_end=row.get("span_end"),
        category=row.get("category"),
        note=row.get("note") or "",
        payload=row.get("payload") or {},
        extra=row.get("extra") or {},
        staged=row.get("staged", False),
        created_at=datetime.fromisoformat(row["created_at"]),
    )
