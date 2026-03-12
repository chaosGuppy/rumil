"""
FastAPI application for the Differential research workspace.

Read-only API for browsing projects, pages, links, and calls.
"""

import logging
import os
import uuid

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import TypeAdapter, ValidationError

from differential.database import DB
from differential.models import Call, Page, PageLink, PageType, Project, Workspace
from differential.settings import get_settings
from differential.api.schemas import (
    CallTraceOut,
    ConsiderationOut,
    LLMExchangeOut,
    LLMExchangeSummaryOut,
    PageCountsOut,
    QuestionTreeOut,
    RealtimeConfigOut,
    RunSummaryOut,
    RunTraceOut,
    TraceEventOut,
)

log = logging.getLogger(__name__)
_trace_event_adapter = TypeAdapter(TraceEventOut)


app = FastAPI(
    title="Differential API",
    version="0.1.0",
    description="Read-only API for the Differential research workspace.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_methods=["GET"],
    allow_headers=["*"],
)


async def _get_db(project_id: str = "") -> DB:
    prod = get_settings().is_prod_db
    return await DB.create(
        run_id=str(uuid.uuid4()),
        prod=prod,
        project_id=project_id,
    )


# --- Projects ---

@app.get("/api/projects", response_model=list[Project])
async def list_projects():
    db = await _get_db()
    return await db.list_projects()


# --- Pages ---

@app.get("/api/projects/{project_id}/pages", response_model=list[Page])
async def list_pages(
    project_id: str,
    page_type: PageType | None = None,
    workspace: Workspace | None = None,
    active_only: bool = True,
):
    db = await _get_db(project_id)
    return await db.get_pages(
        workspace=workspace,
        page_type=page_type,
        active_only=active_only,
    )


@app.get("/api/pages/{page_id}", response_model=Page)
async def get_page(page_id: str):
    db = await _get_db()
    page = await db.get_page(page_id)
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")
    return page


@app.get("/api/pages/{page_id}/links/from", response_model=list[PageLink])
async def get_links_from(page_id: str):
    db = await _get_db()
    return await db.get_links_from(page_id)


@app.get("/api/pages/{page_id}/links/to", response_model=list[PageLink])
async def get_links_to(page_id: str):
    db = await _get_db()
    return await db.get_links_to(page_id)


@app.get("/api/pages/{page_id}/counts", response_model=PageCountsOut)
async def get_page_counts(page_id: str):
    db = await _get_db()
    counts = await db.count_pages_for_question(page_id)
    return PageCountsOut(**counts)


# --- Questions ---

@app.get(
    "/api/projects/{project_id}/questions",
    response_model=list[Page],
)
async def list_root_questions(
    project_id: str,
    workspace: Workspace = Workspace.RESEARCH,
):
    db = await _get_db(project_id)
    return await db.get_root_questions(workspace)


@app.get(
    "/api/questions/{question_id}/tree",
    response_model=QuestionTreeOut,
)
async def get_question_tree(question_id: str, depth: int = Query(default=2, ge=1, le=5)):
    db = await _get_db()
    return await _build_question_tree(db, question_id, depth)


async def _build_question_tree(db: DB, question_id: str, depth: int) -> QuestionTreeOut:
    page = await db.get_page(question_id)
    if not page:
        raise HTTPException(status_code=404, detail="Question not found")

    considerations_raw = await db.get_considerations_for_question(question_id)
    considerations = [
        ConsiderationOut(page=p, link=l)
        for p, l in considerations_raw
    ]

    judgements = await db.get_judgements_for_question(question_id)

    child_questions: list[QuestionTreeOut] = []
    if depth > 1:
        children = await db.get_child_questions(question_id)
        child_questions = [
            await _build_question_tree(db, c.id, depth - 1) for c in children
        ]

    return QuestionTreeOut(
        question=page,
        considerations=considerations,
        judgements=judgements,
        child_questions=child_questions,
    )


# --- Calls ---

@app.get(
    "/api/projects/{project_id}/calls",
    response_model=list[Call],
)
async def list_calls(
    project_id: str,
    question_id: str | None = None,
):
    db = await _get_db(project_id)
    if question_id:
        return await db.get_root_calls_for_question(question_id)
    from differential.database import _rows, _row_to_call
    rows = _rows(
        await db.client.table("calls")
        .select("*")
        .eq("project_id", project_id)
        .order("created_at", desc=True)
        .limit(100)
        .execute()
    )
    return [_row_to_call(r) for r in rows]


@app.get("/api/calls/{call_id}", response_model=Call)
async def get_call(call_id: str):
    db = await _get_db()
    call = await db.get_call(call_id)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")
    return call


@app.get("/api/calls/{call_id}/children", response_model=list[Call])
async def get_child_calls(call_id: str):
    db = await _get_db()
    return await db.get_child_calls(call_id)


async def _build_call_trace(db: DB, call_id: str) -> CallTraceOut:
    call = await db.get_call(call_id)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")
    trace_events = await db.get_call_trace(call_id)
    events = []
    for e in trace_events:
        if "data" in e and isinstance(e["data"], dict):
            e = {k: v for k, v in e.items() if k != "data"} | e["data"]
        e.setdefault("call_id", call_id)
        try:
            events.append(_trace_event_adapter.validate_python(e))
        except ValidationError:
            log.warning("Skipping unrecognised trace event: %s", e.get("event"))
    children = await db.get_child_calls(call_id)
    child_traces = [await _build_call_trace(db, c.id) for c in children]
    return CallTraceOut(
        call=call,
        events=events,
        children=child_traces,
    )


@app.get("/api/runs/{run_id}/trace", response_model=RunTraceOut)
async def get_run_trace(run_id: str):
    db = await _get_db()
    question_id = await db.get_run_question_id(run_id)
    question_page = None
    if question_id:
        question_page = await db.get_page(question_id)
    calls = await db.get_calls_for_run(run_id)
    root_calls = [c for c in calls if c.parent_call_id is None]
    root_traces = [await _build_call_trace(db, c.id) for c in root_calls]
    return RunTraceOut(
        run_id=run_id,
        question=question_page,
        root_calls=root_traces,
    )


@app.get("/api/calls/{call_id}/trace", response_model=CallTraceOut)
async def get_call_trace(call_id: str):
    db = await _get_db()
    return await _build_call_trace(db, call_id)


@app.get(
    "/api/calls/{call_id}/llm-exchanges",
    response_model=list[LLMExchangeSummaryOut],
)
async def list_llm_exchanges(call_id: str):
    db = await _get_db()
    rows = await db.get_llm_exchanges(call_id)
    return [
        LLMExchangeSummaryOut(
            id=r["id"],
            phase=r["phase"],
            round=r["round"],
            input_tokens=r.get("input_tokens"),
            output_tokens=r.get("output_tokens"),
            duration_ms=r.get("duration_ms"),
            error=r.get("error"),
            created_at=r["created_at"],
        )
        for r in rows
    ]


@app.get("/api/llm-exchanges/{exchange_id}", response_model=LLMExchangeOut)
async def get_llm_exchange(exchange_id: str):
    db = await _get_db()
    row = await db.get_llm_exchange(exchange_id)
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
        duration_ms=row.get("duration_ms"),
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
async def list_question_runs(question_id: str):
    db = await _get_db()
    runs = await db.get_runs_for_question(question_id)
    return [
        RunSummaryOut(run_id=r["run_id"], created_at=r["created_at"])
        for r in runs
    ]
