"""
FastAPI application for the Rumil research workspace.

Mostly read-only browsing endpoints (projects, pages, links, calls). Also
exposes the `/api/jobs` family for the job-monitoring UI: POST
`/api/jobs/orchestrator-runs` creates a Kubernetes Job to run an
orchestrator investigation remotely, and GET `/api/jobs` lists recent
orchestrator Jobs in the cluster. See `rumil.api.jobs`.
"""

import logging
import os
import uuid
from collections.abc import AsyncIterator

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import TypeAdapter, ValidationError

from rumil.api.auth import AuthUser, get_current_user, is_admin
from rumil.api.jobs import router as jobs_router
from rumil.api.schemas import (
    ABEvalDimensionOut,
    ABEvalDimensionSummaryOut,
    ABEvalReportListItemOut,
    ABEvalReportOut,
    AuthUserOut,
    CallNodeOut,
    CallSummary,
    LinkedPageOut,
    LLMExchangeOut,
    LLMExchangeSummaryOut,
    PageCountsOut,
    PageDetailOut,
    PageLoadEventOut,
    PageLoadStatsOut,
    PaginatedPagesOut,
    ProjectStatsOut,
    QuestionStatsOut,
    RealtimeConfigOut,
    RunListItemOut,
    RunSummaryOut,
    RunTraceTreeOut,
    TraceEventOut,
)
from rumil.database import DB, _row_to_call, _rows
from rumil.models import Call, Page, PageLink, PageType, Project, Workspace
from rumil.settings import get_settings

log = logging.getLogger(__name__)
_trace_event_adapter = TypeAdapter(TraceEventOut)


app = FastAPI(
    title="Rumil API",
    version="0.1.0",
    description=(
        "Read-only browsing API for the Rumil research workspace, plus the "
        "/api/jobs endpoints used by the job-monitoring UI: POST "
        "/api/jobs/orchestrator-runs submits a Kubernetes Job and GET "
        "/api/jobs lists recent orchestrator Jobs in the cluster."
    ),
)

_ALLOWED_FRONTEND_ORIGINS = [
    o.strip()
    for o in os.environ.get(
        "RUMIL_ALLOWED_ORIGINS",
        "http://localhost:3000,http://127.0.0.1:3000,http://localhost:3012,http://localhost:3013",
    ).split(",")
    if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_FRONTEND_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(jobs_router)


async def _assert_page_access(db: DB, page_id: str) -> Page:
    page = await db.get_page(page_id)
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")
    return page


async def _assert_call_access(db: DB, call_id: str) -> Call:
    call = await db.get_call(call_id)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")
    return call


async def _assert_run_access(db: DB, run_id: str) -> None:
    rows = _rows(await db.client.table("runs").select("id").eq("id", run_id).execute())
    if not rows:
        raise HTTPException(status_code=404, detail="Run not found")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


async def _get_db(
    project_id: str = "",
    _user: AuthUser = Depends(get_current_user),
) -> AsyncIterator[DB]:
    """Yield a per-request DB, closing its HTTP connections on teardown.

    The `_user` dependency is declared purely as an auth gate — every
    endpoint that injects the DB transitively requires a valid JWT. Project
    visibility is intentionally global: any signed-in user sees all projects.
    """
    prod = get_settings().is_prod_db
    db = await DB.create(
        run_id=str(uuid.uuid4()),
        prod=prod,
        project_id=project_id,
    )
    try:
        yield db
    finally:
        await db.close()


async def _get_db_maybe_staged(
    staged_run_id: str | None = None,
    project_id: str = "",
    _user: AuthUser = Depends(get_current_user),
) -> AsyncIterator[DB]:
    """Same as `_get_db` but optionally scoped to a staged run."""
    prod = get_settings().is_prod_db
    if staged_run_id:
        db = await DB.create(
            run_id=staged_run_id,
            prod=prod,
            project_id=project_id,
            staged=True,
        )
    else:
        db = await DB.create(
            run_id=str(uuid.uuid4()),
            prod=prod,
            project_id=project_id,
        )
    try:
        yield db
    finally:
        await db.close()


async def _get_admin_db(
    _user: AuthUser = Depends(get_current_user),
) -> AsyncIterator[DB]:
    """A no-query-param DB factory for admin-status lookups.

    `_get_db` accepts `project_id`/`staged_run_id` as query params; routing
    those through endpoints that don't care (like `/api/auth/me`) leaks
    them into the OpenAPI surface, so admin checks use this instead.
    """
    prod = get_settings().is_prod_db
    db = await DB.create(run_id=str(uuid.uuid4()), prod=prod)
    try:
        yield db
    finally:
        await db.close()


async def require_admin(
    user: AuthUser = Depends(get_current_user),
    db: DB = Depends(_get_admin_db),
) -> AuthUser:
    if not await is_admin(user, db):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


@app.get("/api/auth/me", response_model=AuthUserOut)
async def get_me(
    user: AuthUser = Depends(get_current_user),
    db: DB = Depends(_get_admin_db),
):
    return AuthUserOut(
        user_id=user.user_id,
        email=user.email,
        is_admin=await is_admin(user, db),
    )


@app.get("/api/projects", response_model=list[Project])
async def list_projects(db: DB = Depends(_get_db)):
    return await db.list_projects()


@app.get("/api/projects/{project_id}", response_model=Project)
async def get_project(project_id: str, db: DB = Depends(_get_db)):
    rows = _rows(await db.client.table("projects").select("*").eq("id", project_id).execute())
    if not rows:
        raise HTTPException(status_code=404, detail="Project not found")
    r = rows[0]
    return Project(
        id=r["id"],
        name=r["name"],
        created_at=r["created_at"],
        hidden=r.get("hidden", False),
        owner_user_id=r.get("owner_user_id"),
    )


@app.get("/api/projects/{project_id}/runs", response_model=list[RunListItemOut])
async def list_project_runs(
    project_id: str,
    _admin: AuthUser = Depends(require_admin),
    db: DB = Depends(_get_db),
):
    return await db.list_runs_for_project(project_id)


@app.get("/api/projects/{project_id}/pages", response_model=PaginatedPagesOut)
async def list_pages(
    project_id: str,
    page_type: PageType | None = None,
    workspace: Workspace | None = None,
    active_only: bool = True,
    search: str | None = None,
    offset: int = 0,
    limit: int = 50,
    include_hidden: bool = False,
    db: DB = Depends(_get_db_maybe_staged),
):
    pages, total_count = await db.get_pages_paginated(
        workspace=workspace,
        page_type=page_type,
        active_only=active_only,
        search=search,
        offset=offset,
        limit=limit,
        include_hidden=include_hidden,
    )
    return PaginatedPagesOut(
        items=pages,
        total_count=total_count,
        offset=offset,
        limit=limit,
    )


@app.get("/api/pages/short/{short_id}", response_model=Page)
async def get_page_by_short_id(
    short_id: str,
    user: AuthUser = Depends(get_current_user),
    db: DB = Depends(_get_db_maybe_staged),
):
    full_id = await db.resolve_page_id(short_id)
    if not full_id:
        raise HTTPException(status_code=404, detail="Page not found")
    return await _assert_page_access(db, full_id)


@app.get("/api/pages/{page_id}", response_model=Page)
async def get_page(
    page_id: str,
    user: AuthUser = Depends(get_current_user),
    db: DB = Depends(_get_db_maybe_staged),
):
    return await _assert_page_access(db, page_id)


@app.get("/api/pages/{page_id}/links/from", response_model=list[PageLink])
async def get_links_from(
    page_id: str,
    user: AuthUser = Depends(get_current_user),
    db: DB = Depends(_get_db),
):
    await _assert_page_access(db, page_id)
    return await db.get_links_from(page_id)


@app.get("/api/pages/{page_id}/links/to", response_model=list[PageLink])
async def get_links_to(
    page_id: str,
    user: AuthUser = Depends(get_current_user),
    db: DB = Depends(_get_db),
):
    await _assert_page_access(db, page_id)
    return await db.get_links_to(page_id)


@app.get("/api/pages/{page_id}/dependents", response_model=list[LinkedPageOut])
async def get_dependents(
    page_id: str,
    include_hidden: bool = False,
    user: AuthUser = Depends(get_current_user),
    db: DB = Depends(_get_db),
):
    """Pages that depend on this page (inbound DEPENDS_ON links)."""
    await _assert_page_access(db, page_id)
    results = await db.get_dependents(page_id, include_hidden=include_hidden)
    return [LinkedPageOut(page=page, link=link) for page, link in results]


@app.get("/api/pages/{page_id}/dependencies", response_model=list[LinkedPageOut])
async def get_dependencies(
    page_id: str,
    include_hidden: bool = False,
    user: AuthUser = Depends(get_current_user),
    db: DB = Depends(_get_db),
):
    """Pages that this page depends on (outbound DEPENDS_ON links)."""
    await _assert_page_access(db, page_id)
    results = await db.get_dependencies(page_id, include_hidden=include_hidden)
    return [LinkedPageOut(page=page, link=link) for page, link in results]


@app.get("/api/pages/{page_id}/detail", response_model=PageDetailOut)
async def get_page_detail(
    page_id: str,
    user: AuthUser = Depends(get_current_user),
    db: DB = Depends(_get_db_maybe_staged),
):
    page = await _assert_page_access(db, page_id)
    raw_from = await db.get_links_from(page_id)
    raw_to = await db.get_links_to(page_id)
    all_linked_ids = [link.to_page_id for link in raw_from] + [link.from_page_id for link in raw_to]
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
async def get_page_counts(
    page_id: str,
    user: AuthUser = Depends(get_current_user),
    db: DB = Depends(_get_db),
):
    await _assert_page_access(db, page_id)
    counts = await db.count_pages_for_question(page_id)
    return PageCountsOut(**counts)


@app.get("/api/projects/{project_id}/stats", response_model=ProjectStatsOut)
async def get_project_stats(
    project_id: str,
    _admin: AuthUser = Depends(require_admin),
    db: DB = Depends(_get_db_maybe_staged),
):
    """Aggregate stats over all pages/links/calls in a project.

    Baseline rows are always included; when `staged_run_id` is provided as a
    query param, rows from that staged run are also included and its mutation
    events (supersede_page, delete_link) are overlayed.
    """
    blob = await db.get_project_stats(project_id)
    return ProjectStatsOut(project_id=project_id, **blob)


@app.get("/api/pages/{page_id}/stats", response_model=QuestionStatsOut)
async def get_question_stats(
    page_id: str,
    _admin: AuthUser = Depends(require_admin),
    db: DB = Depends(_get_db_maybe_staged),
):
    """Aggregate stats over the 2-hop undirected neighborhood around a question.

    Returns 404 if the target page is not a question. Staged-run visibility
    matches get_project_stats.
    """
    page = await _assert_page_access(db, page_id)
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
    include_hidden: bool = False,
    db: DB = Depends(_get_db),
):
    return await db.get_root_questions(workspace, include_hidden=include_hidden)


@app.get(
    "/api/projects/{project_id}/calls",
    response_model=list[Call],
)
async def list_calls(
    project_id: str,
    question_id: str | None = None,
    _admin: AuthUser = Depends(require_admin),
    db: DB = Depends(_get_db),
):
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
async def get_call(
    call_id: str,
    _admin: AuthUser = Depends(require_admin),
    db: DB = Depends(_get_db),
):
    return await _assert_call_access(db, call_id)


@app.get("/api/calls/{call_id}/children", response_model=list[Call])
async def get_child_calls(
    call_id: str,
    _admin: AuthUser = Depends(require_admin),
    db: DB = Depends(_get_db),
):
    await _assert_call_access(db, call_id)
    return await db.get_child_calls(call_id)


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
async def get_run_trace_tree(
    run_id: str,
    _admin: AuthUser = Depends(require_admin),
    db: DB = Depends(_get_db),
):
    await _assert_run_access(db, run_id)
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
    run_resp = await db.client.table("runs").select("staged, config").eq("id", run_id).execute()
    run_data: list[dict[str, object]] = run_resp.data or []  # type: ignore[assignment]
    is_staged = bool(run_data and run_data[0].get("staged"))
    run_config: dict = {}
    if run_data:
        run_config = run_data[0].get("config") or {}  # type: ignore[assignment]
    return RunTraceTreeOut(
        run_id=run_id,
        question=question_page,
        calls=nodes,
        cost_usd=total_cost if total_cost > 0 else None,
        staged=is_staged,
        config=run_config,
    )


@app.get("/api/calls/{call_id}/events", response_model=list[TraceEventOut])
async def get_call_events(
    call_id: str,
    _admin: AuthUser = Depends(require_admin),
    db: DB = Depends(_get_db),
):
    await _assert_call_access(db, call_id)
    return await _parse_trace_events(db, call_id)


@app.get(
    "/api/ab-evals",
    response_model=list[ABEvalReportListItemOut],
)
async def list_ab_evals(
    _admin: AuthUser = Depends(require_admin),
    db: DB = Depends(_get_db),
):
    rows = await db.list_ab_eval_reports()

    question_ids = {
        qid for row in rows for qid in (row.get("question_id_a"), row.get("question_id_b")) if qid
    }
    pages_by_id: dict[str, Page] = {}
    if question_ids:
        pages_by_id = await db.get_pages_by_ids(list(question_ids))

    results: list[ABEvalReportListItemOut] = []
    for row in rows:
        qid = row.get("question_id_a") or row.get("question_id_b") or ""
        q_page = pages_by_id.get(qid)
        dims = row.get("dimension_reports") or []
        results.append(
            ABEvalReportListItemOut(
                id=row["id"],
                run_id_a=row["run_id_a"],
                run_id_b=row["run_id_b"],
                question_id_a=row.get("question_id_a") or "",
                question_id_b=row.get("question_id_b") or "",
                question_headline=q_page.headline if q_page else "",
                overall_assessment_preview=(row.get("overall_assessment") or "")[:300],
                preferences=[
                    ABEvalDimensionSummaryOut(
                        name=d.get("name", ""),
                        display_name=d.get("display_name", ""),
                        preference=d.get("preference", ""),
                    )
                    for d in dims
                ],
                created_at=row.get("created_at", ""),
            )
        )
    return results


@app.get(
    "/api/ab-evals/{eval_id}",
    response_model=ABEvalReportOut,
)
async def get_ab_eval(
    eval_id: str,
    _admin: AuthUser = Depends(require_admin),
    db: DB = Depends(_get_db),
):
    row = await db.get_ab_eval_report(eval_id)
    if not row:
        raise HTTPException(status_code=404, detail="AB eval report not found")

    qid = row.get("question_id_a") or row.get("question_id_b") or ""
    q_page = await db.get_page(qid) if qid else None
    dims = row.get("dimension_reports") or []

    run_ids = [row["run_id_a"], row["run_id_b"]]
    run_rows = _rows(
        await db.client.table("runs").select("id, config").in_("id", run_ids).execute()
    )
    configs_by_id = {r["id"]: r.get("config") or {} for r in run_rows}

    return ABEvalReportOut(
        id=row["id"],
        run_id_a=row["run_id_a"],
        run_id_b=row["run_id_b"],
        question_id_a=row.get("question_id_a") or "",
        question_id_b=row.get("question_id_b") or "",
        question_headline=q_page.headline if q_page else "",
        overall_assessment=row.get("overall_assessment") or "",
        overall_assessment_call_id=row.get("overall_assessment_call_id") or "",
        dimension_reports=[
            ABEvalDimensionOut(
                name=d.get("name", ""),
                display_name=d.get("display_name", ""),
                preference=d.get("preference", ""),
                report=d.get("report", ""),
                call_id=d.get("call_id", ""),
            )
            for d in dims
        ],
        config_a=configs_by_id.get(row["run_id_a"], {}),
        config_b=configs_by_id.get(row["run_id_b"], {}),
        created_at=row.get("created_at", ""),
    )


@app.get(
    "/api/calls/{call_id}/llm-exchanges",
    response_model=list[LLMExchangeSummaryOut],
)
async def list_llm_exchanges(
    call_id: str,
    _admin: AuthUser = Depends(require_admin),
    db: DB = Depends(_get_db),
):
    await _assert_call_access(db, call_id)
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
async def get_llm_exchange(
    exchange_id: str,
    _admin: AuthUser = Depends(require_admin),
    db: DB = Depends(_get_db),
):
    row = await db.get_llm_exchange(exchange_id)
    if not row:
        raise HTTPException(status_code=404, detail="LLM exchange not found")
    await _assert_call_access(db, row["call_id"])
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
def get_realtime_config(_user: AuthUser = Depends(get_current_user)):
    settings = get_settings()
    url, key = settings.get_supabase_credentials(prod=settings.is_prod_db)
    anon_key = os.environ.get("SUPABASE_ANON_KEY", key)
    return RealtimeConfigOut(url=url, anon_key=anon_key)


@app.get(
    "/api/pages/{page_id}/run",
    response_model=RunSummaryOut | None,
)
async def get_page_run(
    page_id: str,
    _admin: AuthUser = Depends(require_admin),
    db: DB = Depends(_get_db_maybe_staged),
):
    await _assert_page_access(db, page_id)
    run = await db.get_run_for_page(page_id)
    if not run:
        return None
    return RunSummaryOut(
        run_id=run["run_id"],
        created_at=run["created_at"],
        provenance_call_id=run.get("provenance_call_id", ""),
    )


@app.get(
    "/api/runs/{run_id}/page-load-stats",
    response_model=PageLoadStatsOut,
)
async def get_page_load_stats(
    run_id: str,
    _admin: AuthUser = Depends(require_admin),
    db: DB = Depends(_get_db),
):
    await _assert_run_access(db, run_id)
    rows = await db.get_page_format_events_for_run(run_id)

    events = [
        PageLoadEventOut(
            page_id=r["page_id"],
            detail=r["detail"],
            tags=r.get("tags") or {},
        )
        for r in rows
    ]
    unique_pages = {r["page_id"] for r in rows}

    question_shorts = {q for ev in events if (q := ev.tags.get("question"))}
    question_headlines: dict[str, str] = {}
    if question_shorts:
        resolved = await db.resolve_page_ids(list(question_shorts))
        full_ids = list(set(resolved.values()))
        if full_ids:
            page_by_id = await db.get_pages_by_ids(full_ids)
            for short_id, full_id in resolved.items():
                page = page_by_id.get(full_id)
                if page and page.headline:
                    question_headlines[short_id] = page.headline

    return PageLoadStatsOut(
        events=events,
        total=len(rows),
        total_unique=len(unique_pages),
        question_headlines=question_headlines,
    )
