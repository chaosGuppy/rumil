"""
FastAPI application for the Rumil research workspace.

Read-only API for browsing projects, pages, links, and calls.
"""

import asyncio
import base64
import logging
import os
import secrets
import uuid

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import TypeAdapter, ValidationError
from starlette.middleware.base import BaseHTTPMiddleware

from rumil.database import DB, _row_to_call, _rows
from rumil.models import Call, Page, PageLink, PageType, Project, Workspace
from rumil.settings import get_settings
from rumil.api.schemas import (
    ABRunArmOut,
    ABRunTraceOut,
    CallSequenceOut,
    CallTraceOut,
    LinkedPageOut,
    LLMExchangeOut,
    LLMExchangeSummaryOut,
    PageCountsOut,
    PageDetailOut,
    PaginatedPagesOut,
    ProjectStatsOut,
    QuestionStatsOut,
    RealtimeConfigOut,
    RunListItemOut,
    CallNodeOut,
    CallSummary,
    RunSummaryOut,
    RunTraceOut,
    RunTraceTreeOut,
    TraceEventOut,
)

log = logging.getLogger(__name__)
_trace_event_adapter = TypeAdapter(TraceEventOut)


app = FastAPI(
    title="Rumil API",
    version="0.1.0",
    description="Read-only API for the Rumil research workspace.",
)

_AUTH_PASSWORD = os.environ.get("RUMIL_AUTH_PASSWORD", "")


class BasicAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        if request.url.path == "/healthz" or not _AUTH_PASSWORD:
            return await call_next(request)

        auth = request.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth[6:]).decode()
                _, password = decoded.split(":", 1)
            except Exception:
                password = ""
            if secrets.compare_digest(password, _AUTH_PASSWORD):
                return await call_next(request)

        return Response(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="rumil"'},
            content="Unauthorized",
        )


app.add_middleware(BasicAuthMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


async def _get_db(project_id: str = "") -> DB:
    prod = get_settings().is_prod_db
    return await DB.create(
        run_id=str(uuid.uuid4()),
        prod=prod,
        project_id=project_id,
    )


async def _get_db_maybe_staged(
    staged_run_id: str | None = None,
    project_id: str = "",
) -> DB:
    if staged_run_id:
        prod = get_settings().is_prod_db
        return await DB.create(
            run_id=staged_run_id,
            prod=prod,
            project_id=project_id,
            staged=True,
        )
    return await _get_db(project_id)


@app.get("/api/projects", response_model=list[Project])
async def list_projects():
    db = await _get_db()
    return await db.list_projects()


@app.get("/api/projects/{project_id}", response_model=Project)
async def get_project(project_id: str):

    db = await _get_db(project_id)
    rows = _rows(
        await db.client.table("projects").select("*").eq("id", project_id).execute()
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Project not found")
    r = rows[0]
    return Project(
        id=r["id"],
        name=r["name"],
        created_at=r["created_at"],
        hidden=r.get("hidden", False),
    )


@app.get("/api/projects/{project_id}/runs", response_model=list[RunListItemOut])
async def list_project_runs(project_id: str):
    db = await _get_db(project_id)
    return await db.list_runs_for_project(project_id)


@app.get("/api/projects/{project_id}/pages", response_model=PaginatedPagesOut)
async def list_pages(
    project_id: str,
    page_type: PageType | None = None,
    workspace: Workspace | None = None,
    active_only: bool = True,
    staged_run_id: str | None = None,
    search: str | None = None,
    offset: int = 0,
    limit: int = 50,
):
    db = await _get_db_maybe_staged(staged_run_id, project_id)
    pages, total_count = await db.get_pages_paginated(
        workspace=workspace,
        page_type=page_type,
        active_only=active_only,
        search=search,
        offset=offset,
        limit=limit,
    )
    return PaginatedPagesOut(
        items=pages,
        total_count=total_count,
        offset=offset,
        limit=limit,
    )


@app.get("/api/pages/short/{short_id}", response_model=Page)
async def get_page_by_short_id(short_id: str, staged_run_id: str | None = None):
    db = await _get_db_maybe_staged(staged_run_id)
    full_id = await db.resolve_page_id(short_id)
    if not full_id:
        raise HTTPException(status_code=404, detail="Page not found")
    page = await db.get_page(full_id)
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")
    return page


@app.get("/api/pages/{page_id}", response_model=Page)
async def get_page(page_id: str, staged_run_id: str | None = None):
    db = await _get_db_maybe_staged(staged_run_id)
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


@app.get("/api/pages/{page_id}/dependents", response_model=list[LinkedPageOut])
async def get_dependents(page_id: str):
    """Pages that depend on this page (inbound DEPENDS_ON links)."""
    db = await _get_db()
    results = await db.get_dependents(page_id)
    return [LinkedPageOut(page=page, link=link) for page, link in results]


@app.get("/api/pages/{page_id}/dependencies", response_model=list[LinkedPageOut])
async def get_dependencies(page_id: str):
    """Pages that this page depends on (outbound DEPENDS_ON links)."""
    db = await _get_db()
    results = await db.get_dependencies(page_id)
    return [LinkedPageOut(page=page, link=link) for page, link in results]


@app.get("/api/pages/{page_id}/detail", response_model=PageDetailOut)
async def get_page_detail(page_id: str, staged_run_id: str | None = None):
    db = await _get_db_maybe_staged(staged_run_id)
    page = await db.get_page(page_id)
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")
    raw_from = await db.get_links_from(page_id)
    raw_to = await db.get_links_to(page_id)
    all_linked_ids = [link.to_page_id for link in raw_from] + [
        link.from_page_id for link in raw_to
    ]
    pages_by_id = await db.get_pages_by_ids(all_linked_ids)
    links_from = [
        LinkedPageOut(page=pages_by_id[link.to_page_id], link=link)
        for link in raw_from
        if link.to_page_id in pages_by_id
    ]
    links_to = [
        LinkedPageOut(page=pages_by_id[link.from_page_id], link=link)
        for link in raw_to
        if link.from_page_id in pages_by_id
    ]
    return PageDetailOut(page=page, links_from=links_from, links_to=links_to)


@app.get("/api/pages/{page_id}/counts", response_model=PageCountsOut)
async def get_page_counts(page_id: str):
    db = await _get_db()
    counts = await db.count_pages_for_question(page_id)
    return PageCountsOut(**counts)


@app.get("/api/projects/{project_id}/stats", response_model=ProjectStatsOut)
async def get_project_stats(project_id: str):
    """Aggregate stats over all pages/links/calls in a project.

    v1 is baseline-only: rows with staged=true or is_superseded=true are excluded,
    and staged_run_id is not accepted.
    """
    db = await _get_db(project_id)
    blob = await db.get_project_stats(project_id)
    return ProjectStatsOut(project_id=project_id, **blob)


@app.get("/api/pages/{page_id}/stats", response_model=QuestionStatsOut)
async def get_question_stats(page_id: str):
    """Aggregate stats over the 2-hop undirected neighborhood around a question.

    Returns 404 if the target page is not a question. v1 is baseline-only.
    """
    db = await _get_db()
    page = await db.get_page(page_id)
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")
    if page.page_type != PageType.QUESTION:
        raise HTTPException(
            status_code=404,
            detail="Stats are only available for question pages",
        )
    blob = await db.get_question_stats(page_id)
    return QuestionStatsOut(question_id=page_id, **blob)


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
    events, children, db_sequences = await asyncio.gather(
        _parse_trace_events(db, call_id),
        db.get_child_calls(call_id),
        db.get_sequences_for_call(call_id),
    )
    scope_page_summary = None
    if call.scope_page_id:
        scope_page = await db.get_page(call.scope_page_id)
        if scope_page:
            scope_page_summary = scope_page.headline
    child_traces = list(
        await asyncio.gather(*[_build_call_trace(db, c.id) for c in children])
    )

    sequences_out: list[CallSequenceOut] | None = None
    if db_sequences:
        seq_call_lists = await asyncio.gather(
            *[db.get_calls_for_sequence(seq.id) for seq in db_sequences]
        )
        seq_trace_lists = await asyncio.gather(
            *[
                asyncio.gather(*[_build_call_trace(db, sc.id) for sc in seq_calls])
                for seq_calls in seq_call_lists
            ]
        )
        sequences_out = [
            CallSequenceOut(
                id=seq.id,
                position_in_batch=seq.position_in_batch,
                calls=list(seq_traces),
            )
            for seq, seq_traces in zip(db_sequences, seq_trace_lists)
        ]

    child_costs = [ct.cost_usd for ct in child_traces if ct.cost_usd is not None]
    total = (call.cost_usd or 0) + sum(child_costs)
    return CallTraceOut(
        call=call,
        scope_page_summary=scope_page_summary,
        events=events,
        children=child_traces,
        sequences=sequences_out,
        cost_usd=total if total > 0 else None,
    )


async def _parse_trace_events(db: DB, call_id: str) -> list[TraceEventOut]:
    raw_events = await db.get_call_trace(call_id)
    events: list[TraceEventOut] = []
    for e in raw_events:
        if "data" in e and isinstance(e["data"], dict):
            e = {k: v for k, v in e.items() if k != "data"} | e["data"]
        e.setdefault("call_id", call_id)
        try:
            events.append(_trace_event_adapter.validate_python(e))
        except ValidationError:
            log.warning("Skipping unrecognised trace event: %s", e.get("event"))
    return events


def _count_trace_events(trace_json: list[dict] | None) -> tuple[int, int]:
    warnings = 0
    errors = 0
    for e in trace_json or []:
        event = e.get("event") or (e.get("data") or {}).get("event")
        if event == "warning":
            warnings += 1
        elif event == "error":
            errors += 1
    return warnings, errors


@app.get("/api/runs/{run_id}/trace-tree", response_model=RunTraceTreeOut)
async def get_run_trace_tree(run_id: str):
    db = await _get_db()
    question_id = await db.get_run_question_id(run_id)
    question_page = None
    if question_id:
        question_page = await db.get_page(question_id)
    raw_rows = await db.get_call_rows_for_run(run_id)
    calls = [_row_to_call(r) for r in raw_rows]

    scope_ids = [c.scope_page_id for c in calls if c.scope_page_id]
    scope_pages = await db.get_pages_by_ids(scope_ids)
    scope_summaries = {pid: p.headline for pid, p in scope_pages.items()}

    nodes: list[CallNodeOut] = []
    for c, row in zip(calls, raw_rows):
        warn_count, err_count = _count_trace_events(row.get("trace_json"))
        nodes.append(
            CallNodeOut(
                call=CallSummary.model_validate(c, from_attributes=True),
                scope_page_summary=scope_summaries.get(c.scope_page_id)
                if c.scope_page_id
                else None,
                warning_count=warn_count,
                error_count=err_count,
            )
        )
    total_cost = sum(c.cost_usd or 0 for c in calls)
    run_resp = await db.client.table("runs").select("staged").eq("id", run_id).execute()
    run_data: list[dict[str, object]] = run_resp.data or []  # type: ignore[assignment]
    is_staged = bool(run_data and run_data[0].get("staged"))
    return RunTraceTreeOut(
        run_id=run_id,
        question=question_page,
        calls=nodes,
        cost_usd=total_cost if total_cost > 0 else None,
        staged=is_staged,
    )


@app.get("/api/calls/{call_id}/events", response_model=list[TraceEventOut])
async def get_call_events(call_id: str):
    db = await _get_db()
    call = await db.get_call(call_id)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")
    return await _parse_trace_events(db, call_id)


@app.get("/api/ab-runs/{ab_run_id}/trace", response_model=ABRunTraceOut)
async def get_ab_run_trace(ab_run_id: str):
    db = await _get_db()

    ab_rows = _rows(
        await db.client.table("ab_runs").select("*").eq("id", ab_run_id).execute()
    )
    if not ab_rows:
        raise HTTPException(status_code=404, detail="AB run not found")
    ab_row = ab_rows[0]
    arm_rows = _rows(
        await db.client.table("runs")
        .select("id, name, config, ab_arm")
        .eq("ab_run_id", ab_run_id)
        .order("ab_arm")
        .execute()
    )
    question_page = None
    qid = ab_row.get("question_id")
    if qid:
        question_page = await db.get_page(qid)

    async def _build_arm(arm_row: dict) -> ABRunArmOut:
        run_id = arm_row["id"]
        question_id = await db.get_run_question_id(run_id)
        q_page = None
        if question_id:
            q_page = await db.get_page(question_id)
        calls = await db.get_calls_for_run(run_id)
        root_calls = [c for c in calls if c.parent_call_id is None]
        root_traces = list(
            await asyncio.gather(*[_build_call_trace(db, c.id) for c in root_calls])
        )
        run_costs = [ct.cost_usd for ct in root_traces if ct.cost_usd is not None]
        run_total = sum(run_costs)
        trace = RunTraceOut(
            run_id=run_id,
            question=q_page,
            root_calls=root_traces,
            cost_usd=run_total if run_total > 0 else None,
        )
        return ABRunArmOut(
            run_id=run_id,
            name=arm_row.get("name", ""),
            config=arm_row.get("config", {}),
            trace=trace,
        )

    arms = list(await asyncio.gather(*[_build_arm(arm_row) for arm_row in arm_rows]))
    return ABRunTraceOut(
        ab_run_id=ab_run_id,
        name=ab_row.get("name", ""),
        question=question_page,
        arms=arms,
    )


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
        user_messages=row.get("user_messages"),
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
    settings = get_settings()
    url, key = settings.get_supabase_credentials(prod=settings.is_prod_db)
    anon_key = os.environ.get("SUPABASE_ANON_KEY", key)
    return RealtimeConfigOut(url=url, anon_key=anon_key)


@app.get(
    "/api/pages/{page_id}/run",
    response_model=RunSummaryOut | None,
)
async def get_page_run(page_id: str):
    db = await _get_db()
    run = await db.get_run_for_page(page_id)
    if not run:
        return None
    return RunSummaryOut(
        run_id=run["run_id"],
        created_at=run["created_at"],
        provenance_call_id=run.get("provenance_call_id", ""),
    )
