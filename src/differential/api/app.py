"""
FastAPI application for the Differential research workspace.

Read-only API for browsing projects, pages, links, and calls.
"""

import os
import uuid

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from differential.database import DB
from differential.models import PageType, Workspace
from differential.api.schemas import (
    CallOut,
    CallTraceOut,
    ConsiderationOut,
    LLMExchangeOut,
    LLMExchangeSummaryOut,
    PageCountsOut,
    PageLinkOut,
    PageOut,
    ProjectOut,
    QuestionTreeOut,
    RealtimeConfigOut,
    RunSummaryOut,
    RunTraceOut,
    TraceEventOut,
)


app = FastAPI(
    title="Differential API",
    version="0.1.0",
    description="Read-only API for the Differential research workspace.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


def _get_db(project_id: str = "") -> DB:
    prod = os.environ.get("DIFFERENTIAL_PROD_DB", "").lower() in ("1", "true")
    return DB(
        run_id=str(uuid.uuid4()),
        prod=prod,
        project_id=project_id,
    )


def _page_out(page) -> PageOut:
    return PageOut(
        id=page.id,
        page_type=page.page_type,
        layer=page.layer,
        workspace=page.workspace,
        content=page.content,
        summary=page.summary,
        project_id=page.project_id,
        epistemic_status=page.epistemic_status,
        epistemic_type=page.epistemic_type,
        provenance_model=page.provenance_model,
        provenance_call_type=page.provenance_call_type,
        provenance_call_id=page.provenance_call_id,
        created_at=page.created_at,
        superseded_by=page.superseded_by,
        is_superseded=page.is_superseded,
        extra=page.extra,
    )


def _link_out(link) -> PageLinkOut:
    return PageLinkOut(
        id=link.id,
        from_page_id=link.from_page_id,
        to_page_id=link.to_page_id,
        link_type=link.link_type,
        direction=link.direction,
        strength=link.strength,
        reasoning=link.reasoning,
        created_at=link.created_at,
    )


def _call_out(call) -> CallOut:
    return CallOut(
        id=call.id,
        call_type=call.call_type,
        workspace=call.workspace,
        project_id=call.project_id,
        status=call.status,
        parent_call_id=call.parent_call_id,
        scope_page_id=call.scope_page_id,
        budget_allocated=call.budget_allocated,
        budget_used=call.budget_used,
        context_page_ids=call.context_page_ids,
        result_summary=call.result_summary,
        review_json=call.review_json,
        created_at=call.created_at,
        completed_at=call.completed_at,
    )


# --- Projects ---

@app.get("/api/projects", response_model=list[ProjectOut])
def list_projects():
    db = _get_db()
    projects = db.list_projects()
    return [
        ProjectOut(id=p.id, name=p.name, created_at=p.created_at)
        for p in projects
    ]


# --- Pages ---

@app.get("/api/projects/{project_id}/pages", response_model=list[PageOut])
def list_pages(
    project_id: str,
    page_type: PageType | None = None,
    workspace: Workspace | None = None,
    active_only: bool = True,
):
    db = _get_db(project_id)
    pages = db.get_pages(
        workspace=workspace,
        page_type=page_type,
        active_only=active_only,
    )
    return [_page_out(p) for p in pages]


@app.get("/api/pages/{page_id}", response_model=PageOut)
def get_page(page_id: str):
    db = _get_db()
    page = db.get_page(page_id)
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")
    return _page_out(page)


@app.get("/api/pages/{page_id}/links/from", response_model=list[PageLinkOut])
def get_links_from(page_id: str):
    db = _get_db()
    links = db.get_links_from(page_id)
    return [_link_out(l) for l in links]


@app.get("/api/pages/{page_id}/links/to", response_model=list[PageLinkOut])
def get_links_to(page_id: str):
    db = _get_db()
    links = db.get_links_to(page_id)
    return [_link_out(l) for l in links]


@app.get("/api/pages/{page_id}/counts", response_model=PageCountsOut)
def get_page_counts(page_id: str):
    db = _get_db()
    counts = db.count_pages_for_question(page_id)
    return PageCountsOut(**counts)


# --- Questions ---

@app.get(
    "/api/projects/{project_id}/questions",
    response_model=list[PageOut],
)
def list_root_questions(
    project_id: str,
    workspace: Workspace = Workspace.RESEARCH,
):
    db = _get_db(project_id)
    questions = db.get_root_questions(workspace)
    return [_page_out(q) for q in questions]


@app.get(
    "/api/questions/{question_id}/tree",
    response_model=QuestionTreeOut,
)
def get_question_tree(question_id: str, depth: int = Query(default=2, ge=1, le=5)):
    db = _get_db()
    return _build_question_tree(db, question_id, depth)


def _build_question_tree(db: DB, question_id: str, depth: int) -> QuestionTreeOut:
    page = db.get_page(question_id)
    if not page:
        raise HTTPException(status_code=404, detail="Question not found")

    considerations_raw = db.get_considerations_for_question(question_id)
    considerations = [
        ConsiderationOut(page=_page_out(p), link=_link_out(l))
        for p, l in considerations_raw
    ]

    judgements = [_page_out(j) for j in db.get_judgements_for_question(question_id)]

    child_questions: list[QuestionTreeOut] = []
    if depth > 1:
        children = db.get_child_questions(question_id)
        child_questions = [
            _build_question_tree(db, c.id, depth - 1) for c in children
        ]

    return QuestionTreeOut(
        question=_page_out(page),
        considerations=considerations,
        judgements=judgements,
        child_questions=child_questions,
    )


# --- Calls ---

@app.get(
    "/api/projects/{project_id}/calls",
    response_model=list[CallOut],
)
def list_calls(
    project_id: str,
    question_id: str | None = None,
):
    db = _get_db(project_id)
    if question_id:
        calls = db.get_root_calls_for_question(question_id)
    else:
        # List all calls for project — use raw query since DB doesn't have a list_calls
        from differential.database import _rows, _row_to_call
        rows = _rows(
            db.client.table("calls")
            .select("*")
            .eq("project_id", project_id)
            .order("created_at", desc=True)
            .limit(100)
            .execute()
        )
        calls = [_row_to_call(r) for r in rows]
    return [_call_out(c) for c in calls]


@app.get("/api/calls/{call_id}", response_model=CallOut)
def get_call(call_id: str):
    db = _get_db()
    call = db.get_call(call_id)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")
    return _call_out(call)


@app.get("/api/calls/{call_id}/children", response_model=list[CallOut])
def get_child_calls(call_id: str):
    db = _get_db()
    calls = db.get_child_calls(call_id)
    return [_call_out(c) for c in calls]


def _build_call_trace(db: DB, call_id: str) -> CallTraceOut:
    call = db.get_call(call_id)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")
    trace_events = db.get_call_trace(call_id)
    events = [
        TraceEventOut(
            event=e.get("event", ""),
            ts=e.get("ts", ""),
            call_id=e.get("call_id", call_id),
            data=e.get("data", {}),
        )
        for e in trace_events
    ]
    children = db.get_child_calls(call_id)
    child_traces = [_build_call_trace(db, c.id) for c in children]
    return CallTraceOut(
        call=_call_out(call),
        events=events,
        children=child_traces,
    )


@app.get("/api/runs/{run_id}/trace", response_model=RunTraceOut)
def get_run_trace(run_id: str):
    db = _get_db()
    question_id = db.get_run_question_id(run_id)
    question_page = None
    if question_id:
        page = db.get_page(question_id)
        if page:
            question_page = _page_out(page)
    calls = db.get_calls_for_run(run_id)
    root_calls = [c for c in calls if c.parent_call_id is None]
    root_traces = [_build_call_trace(db, c.id) for c in root_calls]
    return RunTraceOut(
        run_id=run_id,
        question=question_page,
        root_calls=root_traces,
    )


@app.get("/api/calls/{call_id}/trace", response_model=CallTraceOut)
def get_call_trace(call_id: str):
    db = _get_db()
    return _build_call_trace(db, call_id)


@app.get(
    "/api/calls/{call_id}/llm-exchanges",
    response_model=list[LLMExchangeSummaryOut],
)
def list_llm_exchanges(call_id: str):
    db = _get_db()
    rows = db.get_llm_exchanges(call_id)
    return [
        LLMExchangeSummaryOut(
            id=r["id"],
            phase=r["phase"],
            round=r["round"],
            input_tokens=r.get("input_tokens"),
            output_tokens=r.get("output_tokens"),
            error=r.get("error"),
            created_at=r["created_at"],
        )
        for r in rows
    ]


@app.get("/api/llm-exchanges/{exchange_id}", response_model=LLMExchangeOut)
def get_llm_exchange(exchange_id: str):
    db = _get_db()
    row = db.get_llm_exchange(exchange_id)
    if not row:
        raise HTTPException(status_code=404, detail="LLM exchange not found")
    return LLMExchangeOut(
        id=row["id"],
        call_id=row["call_id"],
        phase=row["phase"],
        round=row["round"],
        system_prompt=row.get("system_prompt"),
        user_message=row.get("user_message"),
        response_text=row.get("response_text"),
        tool_calls=row.get("tool_calls", []),
        input_tokens=row.get("input_tokens"),
        output_tokens=row.get("output_tokens"),
        error=row.get("error"),
        created_at=row["created_at"],
    )


@app.get("/api/realtime/config", response_model=RealtimeConfigOut)
def get_realtime_config():
    url = os.environ.get("SUPABASE_URL", "http://127.0.0.1:54321")
    anon_key = os.environ.get(
        "SUPABASE_ANON_KEY",
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJpc3MiOiJzdXBhYmFzZS1kZW1vIiwicm9sZSI6ImFub24iLCJleHAiOjE5ODM4MTI5OTZ9."
        "CRXP1A7WO_o0BQXhz7hELn2KrME8ok-w_jA9lFk-VTk",
    )
    return RealtimeConfigOut(url=url, anon_key=anon_key)


@app.get(
    "/api/questions/{question_id}/runs",
    response_model=list[RunSummaryOut],
)
def list_question_runs(question_id: str):
    db = _get_db()
    runs = db.get_runs_for_question(question_id)
    return [
        RunSummaryOut(run_id=r["run_id"], created_at=r["created_at"])
        for r in runs
    ]
