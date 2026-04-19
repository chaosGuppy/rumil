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
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, TypeAdapter, ValidationError
from starlette.middleware.base import BaseHTTPMiddleware

from rumil.api.chat import (
    BranchConversationRequest,
    ChatRequest,
    ChatResponse,
    ConversationDetail,
    ConversationListItem,
    CreateConversationRequest,
    UpdateConversationRequest,
    _derive_title,
    handle_chat,
    handle_chat_stream,
)
from rumil.api.schemas import (
    ABEvalDimensionOut,
    ABEvalDimensionSummaryOut,
    ABEvalReportListItemOut,
    ABEvalReportOut,
    AdversarialVerdictSummaryOut,
    AnnotationCreateOut,
    AnnotationCreateRequest,
    AppConfigOut,
    CallNodeOut,
    CallSummary,
    CreateProjectOut,
    CreateProjectRequest,
    CreateRootQuestionRequest,
    LinkedPageOut,
    LLMExchangeOut,
    LLMExchangeSummaryOut,
    PageCountsOut,
    PageDetailOut,
    PageIterationsOut,
    PageLoadEventOut,
    PageLoadStatsOut,
    PaginatedPagesOut,
    ProjectStatsOut,
    ProjectSummaryOut,
    QuestionStatsOut,
    RealtimeConfigOut,
    RefineIterationOut,
    RefineIterationVerdictOut,
    ReputationBucketOut,
    ReputationSummaryOut,
    RunListItemOut,
    RunSpendByCallTypeOut,
    RunSpendOut,
    RunSummaryOut,
    RunTraceTreeOut,
    SearchResultOut,
    SearchResultsOut,
    TraceEventOut,
    UpdateProjectRequest,
    UpdateRunRequest,
    ViewItemFlagDeleteOut,
    ViewItemFlagOut,
    ViewItemFlagRequest,
    ViewItemReadOut,
    ViewItemReadRequest,
)
from rumil.calls.adversarial_review import AdversarialVerdict, is_verdict_expired
from rumil.database import DB, _row_to_call, _rows
from rumil.embeddings import embed_and_store_page
from rumil.models import (
    AnnotationEvent,
    Call,
    ChatMessageRole,
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Project,
    ReputationEvent,
    Suggestion,
    SuggestionStatus,
    SuggestionType,
    Workspace,
)
from rumil.orchestrators import Orchestrator
from rumil.settings import get_settings
from rumil.views import View, build_view

log = logging.getLogger(__name__)
_trace_event_adapter = TypeAdapter(TraceEventOut)

# Strong refs to fire-and-forget background tasks (orchestrator runs, AB
# evals) so the event loop can't GC them mid-execution. Tasks deregister
# themselves via add_done_callback.
_live_background_tasks: set[asyncio.Task] = set()


def _track_background(task: asyncio.Task) -> None:
    _live_background_tasks.add(task)
    task.add_done_callback(_live_background_tasks.discard)


app = FastAPI(
    title="Rumil API",
    version="0.1.0",
    description="Read-only API for the Rumil research workspace.",
)

_AUTH_PASSWORD = os.environ.get("RUMIL_AUTH_PASSWORD", "")
_FRIENDLY_USER_PASSWORD = os.environ.get("FRIENDLY_USER_PASSWORD", "")


def _is_friendly_user_path(method: str, path: str) -> bool:
    """Return True if `method path` is in the friendly-user read/flag/telemetry
    whitelist.

    Friendly users see only the read surface of a View:
    - read the View for a question
    - get the feature-flag config needed by that page
    - fetch a single page body by id/short-id (rendering helper)
    - flag a view item, undo a flag, record a read-event

    Everything else (projects/*, calls/*, traces/*, runs/*, chat/*, ab-evals/*,
    dispatch, etc.) requires the admin password.
    """
    method = method.upper()
    if method == "GET":
        if path == "/api/config":
            return True
        if path.startswith("/api/questions/") and path.endswith("/view"):
            return True
        if path.startswith("/api/pages/short/"):
            return True
        if path.startswith("/api/pages/") and path.count("/") == 3:
            return True
        if path == "/api/pages/annotations":
            return True
        if path.startswith("/api/pages/") and path.endswith("/annotations"):
            return True
        if path.startswith("/api/calls/") and path.endswith("/annotations"):
            return True
    if method == "POST":
        if path.startswith("/api/view-items/") and (
            path.endswith("/flag") or path.endswith("/read")
        ):
            return True
        if path == "/api/annotations":
            return True
    if method == "DELETE":
        if path.startswith("/api/view-items/flags/"):
            return True
    return False


class BasicAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        path = request.url.path
        if path == "/healthz":
            return await call_next(request)
        if not _AUTH_PASSWORD and not _FRIENDLY_USER_PASSWORD:
            return await call_next(request)

        auth = request.headers.get("Authorization", "")
        password = ""
        if auth.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth[6:]).decode()
                _, password = decoded.split(":", 1)
            except Exception:
                password = ""

        if _AUTH_PASSWORD and secrets.compare_digest(password, _AUTH_PASSWORD):
            return await call_next(request)

        if (
            _FRIENDLY_USER_PASSWORD
            and secrets.compare_digest(password, _FRIENDLY_USER_PASSWORD)
            and _is_friendly_user_path(request.method, path)
        ):
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
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


async def _get_db(project_id: str = "") -> AsyncIterator[DB]:
    """Yield a per-request DB, closing its HTTP connections on teardown.

    FastAPI injects this as a Depends() and runs the post-yield cleanup
    after the response is sent, regardless of whether the endpoint
    returned normally or raised. This is the fundamental lifecycle
    fix for chaosGuppy/rumil#274. project_id, if declared as a path
    parameter on the endpoint, flows in here via name-based matching.
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
) -> AsyncIterator[DB]:
    """Same as _get_db but optionally scoped to a staged run.

    When staged_run_id is present (as a query parameter on the endpoint),
    the DB is constructed with staged=True and run_id=staged_run_id so
    staged-run visibility rules apply.
    """
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


@app.get("/api/projects", response_model=list[Project])
async def list_projects(db: DB = Depends(_get_db)):
    return await db.list_projects()


@app.post("/api/projects", response_model=CreateProjectOut)
async def create_project(
    request: CreateProjectRequest,
    db: DB = Depends(_get_db),
):
    """Create a new workspace (project) from the UI.

    Idempotent: if a workspace with the same trimmed name already exists we
    return it with ``created=false`` so the client can show a subtle
    "already exists" hint while still navigating the user into the existing
    workspace.
    """
    name = request.name.strip()
    if not name:
        raise HTTPException(
            status_code=422,
            detail="Workspace name must not be empty or whitespace-only.",
        )
    project, created = await db.get_or_create_project(name)
    return CreateProjectOut(project=project, created=created)


@app.get("/api/projects/summary", response_model=list[ProjectSummaryOut])
async def list_projects_summary(
    include_hidden: bool = False,
    db: DB = Depends(_get_db),
):
    """Per-project summary for the public landing page.

    One SQL call produces every project's question_count, claim_count,
    call_count, and last_activity_at. Consumed by the parma landing grid.
    ``include_hidden`` is off by default; the parma "show hidden" toggle
    passes ``?include_hidden=true`` to surface soft-deleted workspaces
    (rendered greyed).
    """
    rows = await db.list_projects_summary(include_hidden=include_hidden)
    return [
        ProjectSummaryOut(
            id=row["project_id"],
            name=row["name"],
            created_at=row["created_at"],
            hidden=row.get("hidden", False),
            question_count=row.get("question_count", 0),
            claim_count=row.get("claim_count", 0),
            call_count=row.get("call_count", 0),
            last_activity_at=row["last_activity_at"],
        )
        for row in rows
    ]


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
    )


@app.patch("/api/projects/{project_id}", response_model=Project)
async def update_project(
    project_id: str,
    request: UpdateProjectRequest,
    db: DB = Depends(_get_db),
):
    """Update a workspace's hidden flag and/or name.

    Accepts any subset of ``{hidden, name}``. At least one field must be
    supplied (422 if neither is present). ``name`` is trimmed server-side;
    empty-after-trim is rejected (422) and a collision with another project's
    name returns 409. Renaming to the same name is a no-op (200).

    Backs Feature 1 (hide workspace) and Feature 3 (rename workspace) in the
    parma hygiene landing-pad.
    """
    if request.hidden is None and request.name is None:
        raise HTTPException(
            status_code=422,
            detail="At least one of 'hidden' or 'name' must be provided.",
        )

    existing = await db.get_project(project_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Project not found")

    trimmed_name: str | None = None
    if request.name is not None:
        trimmed_name = request.name.strip()
        if not trimmed_name:
            raise HTTPException(
                status_code=422,
                detail="Workspace name must not be empty or whitespace-only.",
            )
        if trimmed_name != existing.name:
            collision_rows = _rows(
                await db.client.table("projects").select("id").eq("name", trimmed_name).execute()
            )
            if any(r["id"] != project_id for r in collision_rows):
                raise HTTPException(
                    status_code=409,
                    detail=(f"A workspace named '{trimmed_name}' already exists."),
                )

    refreshed = await db.update_project(
        project_id,
        name=trimmed_name,
        hidden=request.hidden,
    )
    assert refreshed is not None
    return refreshed


@app.get("/api/projects/{project_id}/runs", response_model=list[RunListItemOut])
async def list_project_runs(project_id: str, db: DB = Depends(_get_db)):
    return await db.list_runs_for_project(project_id)


@app.patch("/api/runs/{run_id}")
async def update_run(
    run_id: str,
    request: UpdateRunRequest,
    db: DB = Depends(_get_db),
):
    """Update a run's hidden flag.

    Today the only patchable field is ``hidden`` — a soft-delete for the
    run picker so noise runs (smoke tests, failed experiments) don't clutter
    the list. 422 if the request body is empty. 404 if the run doesn't exist.
    """
    if request.hidden is None:
        raise HTTPException(
            status_code=422,
            detail="Body must include 'hidden'.",
        )
    existing = await db.get_run(run_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Run not found")
    refreshed = await db.update_run_hidden(run_id, request.hidden)
    assert refreshed is not None
    return {
        "run_id": run_id,
        "hidden": refreshed.get("hidden", False),
    }


def _extract_snippet(content: str, query: str, window: int = 200) -> str:
    """Return a ~window-char excerpt around the first case-insensitive match.

    Falls back to the leading prefix when the query only matched the headline
    (or the content is shorter than the window). Collapses newlines so the
    excerpt renders cleanly in a dropdown without wrapping pathologically.
    """
    text = (content or "").replace("\n", " ").strip()
    if not text:
        return ""
    if not query:
        return text[:window]
    lowered = text.lower()
    idx = lowered.find(query.lower())
    if idx < 0:
        return text[:window]
    half = window // 2
    start = max(0, idx - half)
    end = min(len(text), start + window)
    if end - start < window:
        start = max(0, end - window)
    excerpt = text[start:end]
    if start > 0:
        excerpt = "..." + excerpt
    if end < len(text):
        excerpt = excerpt + "..."
    return excerpt


@app.get(
    "/api/projects/{project_id}/search",
    response_model=SearchResultsOut,
)
async def search_workspace(
    project_id: str,
    q: str = "",
    limit: int = 30,
    db: DB = Depends(_get_db),
):
    """Case-insensitive ILIKE search across pages.headline and pages.content,
    scoped to one project.

    The snippet is a ~200 char window around the first content match (or the
    leading prefix if the match was headline-only). Non-active (superseded)
    pages are excluded so callers never land on stale drafts. Limit is
    clamped to [1, 100].
    """
    query = q.strip()
    if not query:
        return SearchResultsOut(results=[])
    limit = max(1, min(limit, 100))
    pages, _total = await db.get_pages_paginated(
        search=query,
        active_only=True,
        offset=0,
        limit=limit,
    )
    return SearchResultsOut(
        results=[SearchResultOut(page=p, snippet=_extract_snippet(p.content, query)) for p in pages]
    )


@app.get("/api/projects/{project_id}/pages", response_model=PaginatedPagesOut)
async def list_pages(
    project_id: str,
    page_type: PageType | None = None,
    workspace: Workspace | None = None,
    active_only: bool = True,
    search: str | None = None,
    offset: int = 0,
    limit: int = 50,
    db: DB = Depends(_get_db_maybe_staged),
):
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
async def get_page_by_short_id(
    short_id: str,
    db: DB = Depends(_get_db_maybe_staged),
):
    full_id = await db.resolve_page_id(short_id)
    if not full_id:
        raise HTTPException(status_code=404, detail="Page not found")
    page = await db.get_page(full_id)
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")
    return page


_ANNOTATIONS_BATCH_MAX = 200


@app.get("/api/pages/annotations", response_model=dict[str, list[AnnotationEvent]])
async def list_pages_annotations_batch(
    ids: str = "",
    db: DB = Depends(_get_db_maybe_staged),
):
    """Batched annotation fetch for many pages in a single request.

    Replaces the N-parallel per-page fetch parma previously issued when
    rendering a view. One DB query (``in_()`` over ``target_page_id``) is
    used regardless of how many ids are passed. Response shape is
    ``{page_id: [AnnotationEvent, ...]}`` — pages with no annotations are
    still present with an empty list so the frontend doesn't have to
    second-guess.

    Declared before ``/api/pages/{page_id}`` so FastAPI's declaration-order
    routing doesn't swallow the literal ``annotations`` segment as a
    page_id.
    """
    if not ids:
        return {}
    page_ids = [s for s in ids.split(",") if s]
    if len(page_ids) > _ANNOTATIONS_BATCH_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"Too many ids: max {_ANNOTATIONS_BATCH_MAX} per call",
        )
    project_rows = _rows(
        await db.client.table("pages").select("project_id").in_("id", page_ids).execute()
    )
    distinct_projects = {r["project_id"] for r in project_rows if r.get("project_id")}
    if len(distinct_projects) > 1:
        raise HTTPException(
            status_code=400,
            detail="Batch contains pages from multiple projects",
        )
    if distinct_projects:
        sole_project = next(iter(distinct_projects))
        if db.project_id and db.project_id != sole_project:
            raise HTTPException(
                status_code=400,
                detail="Batch pages don't match the requested project_id",
            )
        if not db.project_id:
            db.project_id = sole_project
    return await db.get_annotations_by_target_pages(page_ids)


@app.get("/api/pages/{page_id}", response_model=Page)
async def get_page(page_id: str, db: DB = Depends(_get_db_maybe_staged)):
    page = await db.get_page(page_id)
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")
    return page


@app.get("/api/pages/{page_id}/links/from", response_model=list[PageLink])
async def get_links_from(page_id: str, db: DB = Depends(_get_db)):
    return await db.get_links_from(page_id)


@app.get("/api/pages/{page_id}/links/to", response_model=list[PageLink])
async def get_links_to(page_id: str, db: DB = Depends(_get_db)):
    return await db.get_links_to(page_id)


@app.get("/api/pages/{page_id}/dependents", response_model=list[LinkedPageOut])
async def get_dependents(page_id: str, db: DB = Depends(_get_db)):
    """Pages that depend on this page (inbound DEPENDS_ON links)."""
    results = await db.get_dependents(page_id)
    return [LinkedPageOut(page=page, link=link) for page, link in results]


@app.get("/api/pages/{page_id}/dependencies", response_model=list[LinkedPageOut])
async def get_dependencies(page_id: str, db: DB = Depends(_get_db)):
    """Pages that this page depends on (outbound DEPENDS_ON links)."""
    results = await db.get_dependencies(page_id)
    return [LinkedPageOut(page=page, link=link) for page, link in results]


@app.get("/api/pages/{page_id}/detail", response_model=PageDetailOut)
async def get_page_detail(
    page_id: str,
    db: DB = Depends(_get_db_maybe_staged),
):
    page = await db.get_page(page_id)
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")
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
async def get_page_counts(page_id: str, db: DB = Depends(_get_db)):
    counts = await db.count_pages_for_question(page_id)
    return PageCountsOut(**counts)


def _extract_iteration_verdict(
    verdict_page: Page | None,
) -> RefineIterationVerdictOut | None:
    """Pull the adversarial_verdict block off a JUDGEMENT page, if parseable.

    Returns ``None`` when the page is missing, isn't a JUDGEMENT, has no
    adversarial_verdict payload, or the payload fails to parse as an
    ``AdversarialVerdict`` (e.g. schema drift). The iteration diff panel
    handles ``verdict=null`` by just suppressing the header chip.
    """
    if verdict_page is None or verdict_page.page_type != PageType.JUDGEMENT:
        return None
    raw = (verdict_page.extra or {}).get("adversarial_verdict")
    if not isinstance(raw, dict):
        return None
    try:
        verdict = AdversarialVerdict.model_validate(raw)
    except ValidationError:
        return None
    return RefineIterationVerdictOut(
        claim_holds=verdict.claim_holds,
        claim_confidence=verdict.claim_confidence,
        dissents=list(verdict.dissents),
        concurrences=list(verdict.concurrences),
        stronger_side=str(verdict.stronger_side),
    )


@app.get("/api/pages/{page_id}/iterations", response_model=PageIterationsOut)
async def get_page_iterations(page_id: str, db: DB = Depends(_get_db)):
    """Walk the draft chain of a refine-artifact run and render each pass.

    The final artifact page carries ``extra['refinement']`` with the
    iteration count and final verdict. Every prior draft was superseded by
    this page (flat, not chained — see _supersede_prior_drafts in
    refine_artifact.py), so we can pull them all in one query. For each
    draft we locate the adversarial_review verdict JUDGEMENT via its
    DEPENDS_ON link back at the draft; ``extra['target_page_id']`` on the
    JUDGEMENT is the tie-breaker when multiple dependents exist.

    Returns 400 when the page isn't an artifact, isn't the accepted
    final (no refinement metadata), or the draft-chain query comes back
    empty for a >1-iteration run.
    """
    page = await db.get_page(page_id)
    if page is None:
        raise HTTPException(status_code=404, detail="Page not found")
    if page.page_type != PageType.ARTIFACT:
        raise HTTPException(
            status_code=400,
            detail=f"Page {page_id[:8]} is not an artifact (type={page.page_type.value}).",
        )
    refinement = (page.extra or {}).get("refinement")
    if not isinstance(refinement, dict):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Artifact {page_id[:8]} has no refinement metadata; "
                "iterations are only recorded on finalized refine-artifact runs."
            ),
        )

    prior_rows = _rows(
        await db._execute(db.client.table("pages").select("id").eq("superseded_by", page_id))
    )
    prior_ids = [r["id"] for r in prior_rows]
    prior_pages_map = await db.get_pages_by_ids(prior_ids) if prior_ids else {}
    draft_pages: list[Page] = [*prior_pages_map.values(), page]
    draft_pages.sort(key=lambda p: p.created_at)
    draft_ids = [p.id for p in draft_pages]

    links_by_target = await db.get_links_to_many(draft_ids)
    candidate_verdict_ids: set[str] = set()
    for links in links_by_target.values():
        for link in links:
            if link.link_type == LinkType.DEPENDS_ON:
                candidate_verdict_ids.add(link.from_page_id)
    verdict_pages = (
        await db.get_pages_by_ids(list(candidate_verdict_ids)) if candidate_verdict_ids else {}
    )

    # Pair each draft with its verdict page. A JUDGEMENT's
    # extra['target_page_id'] is the authoritative link back to the draft
    # (the adversarial_review call sets it). Keep only the newest verdict
    # per draft so re-reviews don't produce duplicates.
    verdicts_by_draft: dict[str, Page] = {}
    for vp in verdict_pages.values():
        if vp.page_type != PageType.JUDGEMENT:
            continue
        target = (vp.extra or {}).get("target_page_id")
        if not isinstance(target, str):
            continue
        if target not in draft_ids:
            continue
        prev = verdicts_by_draft.get(target)
        if prev is None or vp.created_at > prev.created_at:
            verdicts_by_draft[target] = vp

    iterations = [
        RefineIterationOut(
            iteration=idx + 1,
            draft_page_id=draft.id,
            draft_short_id=draft.id[:8],
            content=draft.content,
            headline=draft.headline,
            verdict=_extract_iteration_verdict(verdicts_by_draft.get(draft.id)),
            created_at=draft.created_at,
        )
        for idx, draft in enumerate(draft_pages)
    ]
    return PageIterationsOut(page_id=page_id, iterations=iterations)


async def _collect_adversarial_verdicts(
    db: DB,
    page_ids: Sequence[str],
) -> dict[str, list[AdversarialVerdictSummaryOut]]:
    """Return ``{page_id: [verdicts]}`` for every id in *page_ids*.

    Resolves short/full ids, walks incoming ``DEPENDS_ON`` links in one
    batched fetch, bulk-fetches the candidate JUDGEMENT source pages, and
    returns only those whose ``extra['adversarial_verdict']`` parses cleanly
    as an ``AdversarialVerdict`` payload. This is the batched path behind
    both the single-page and bulk endpoints, so the frontend can call with
    one round-trip for a whole view.
    """
    if not page_ids:
        return {}

    resolved_map = await db.resolve_page_ids(list(page_ids))
    if not resolved_map:
        return {pid: [] for pid in page_ids}
    resolved_ids = list(dict.fromkeys(resolved_map.values()))

    links_by_target = await db.get_links_to_many(resolved_ids)

    candidate_ids: set[str] = set()
    for links in links_by_target.values():
        for link in links:
            if link.link_type == LinkType.DEPENDS_ON:
                candidate_ids.add(link.from_page_id)

    pages_by_id = await db.get_pages_by_ids(list(candidate_ids)) if candidate_ids else {}

    summaries_by_verdict_page: dict[str, AdversarialVerdictSummaryOut] = {}
    for verdict_page_id, page in pages_by_id.items():
        if page.page_type != PageType.JUDGEMENT:
            continue
        raw = (page.extra or {}).get("adversarial_verdict")
        if not isinstance(raw, dict):
            continue
        try:
            verdict = AdversarialVerdict.model_validate(raw)
        except ValidationError:
            log.warning("Skipping unparseable adversarial verdict on page %s", page.id[:8])
            continue
        expired = is_verdict_expired(verdict)
        summaries_by_verdict_page[verdict_page_id] = AdversarialVerdictSummaryOut(
            verdict_page_id=verdict_page_id,
            target_page_id="",
            stronger_side=verdict.stronger_side,
            claim_holds=verdict.claim_holds,
            confidence=verdict.claim_confidence,
            rationale=verdict.rationale,
            concurrences=list(verdict.concurrences),
            dissents=list(verdict.dissents),
            sunset_after_days=verdict.sunset_after_days,
            verdict_created_at=verdict.created_at,
            expired=expired,
            page_created_at=page.created_at,
        )

    result: dict[str, list[AdversarialVerdictSummaryOut]] = {pid: [] for pid in page_ids}
    for input_id, full_id in resolved_map.items():
        per_target: list[AdversarialVerdictSummaryOut] = []
        for link in links_by_target.get(full_id, []):
            if link.link_type != LinkType.DEPENDS_ON:
                continue
            summary = summaries_by_verdict_page.get(link.from_page_id)
            if summary is None:
                continue
            per_target.append(summary.model_copy(update={"target_page_id": full_id}))
        per_target.sort(key=lambda s: s.verdict_created_at, reverse=True)
        result[input_id] = per_target
    return result


@app.get(
    "/api/pages/{page_id}/adversarial-verdicts",
    response_model=list[AdversarialVerdictSummaryOut],
)
async def get_adversarial_verdicts(
    page_id: str,
    db: DB = Depends(_get_db_maybe_staged),
):
    """List adversarial-review verdicts that target *page_id*.

    Verdicts are JUDGEMENT pages with ``extra['adversarial_verdict']`` set,
    linked to the target via a ``DEPENDS_ON`` link (verdict → target).
    Newest verdict first. Expired verdicts are still returned with
    ``expired=true`` so the UI can render them muted.
    """
    verdicts_by_id = await _collect_adversarial_verdicts(db, [page_id])
    return verdicts_by_id.get(page_id, [])


@app.get(
    "/api/adversarial-verdicts",
    response_model=dict[str, list[AdversarialVerdictSummaryOut]],
)
async def get_adversarial_verdicts_batch(
    page_ids: str,
    db: DB = Depends(_get_db_maybe_staged),
):
    """Batched adversarial-verdict lookup. ``page_ids`` is a comma-separated
    list of page ids (full or short). Returns ``{page_id: [verdicts]}`` with
    one entry per input id. Used by the ``useAdversarialVerdicts`` frontend
    hook to avoid N+1 fetches when rendering a whole view.
    """
    ids = [pid.strip() for pid in page_ids.split(",") if pid.strip()]
    if not ids:
        return {}
    return await _collect_adversarial_verdicts(db, ids)


@app.get("/api/projects/{project_id}/stats", response_model=ProjectStatsOut)
async def get_project_stats(project_id: str, db: DB = Depends(_get_db)):
    """Aggregate stats over all pages/links/calls in a project.

    v1 is baseline-only: rows with staged=true or is_superseded=true are excluded,
    and staged_run_id is not accepted.
    """
    blob = await db.get_project_stats(project_id)
    return ProjectStatsOut(project_id=project_id, **blob)


@app.get("/api/pages/{page_id}/stats", response_model=QuestionStatsOut)
async def get_question_stats(page_id: str, db: DB = Depends(_get_db)):
    """Aggregate stats over the 2-hop undirected neighborhood around a question.

    Returns 404 if the target page is not a question. v1 is baseline-only.
    """
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
    db: DB = Depends(_get_db),
):
    return await db.get_root_questions(workspace)


@app.post(
    "/api/projects/{project_id}/questions",
    response_model=Page,
)
async def create_root_question(
    project_id: str,
    request: CreateRootQuestionRequest,
    db: DB = Depends(_get_db),
):
    """Create a bare root question in a workspace.

    No orchestrator dispatch and no call record — just a Page. The UI uses
    this to seed a freshly-created workspace so the user can pose a question
    before firing any research; chat (``/orchestrate``, ``/dispatch``,
    ``/ask``) can then populate it.

    Mirrors the skill-lane pattern in ``ask_question.py`` for field choices:
    ``layer=SQUIDGY``, ``workspace=RESEARCH``, ``provenance_model='human'``,
    and best-effort embedding on the ``abstract`` field so search/dedup work.
    """
    headline = request.headline.strip()
    if not headline:
        raise HTTPException(
            status_code=422,
            detail="Question headline must not be empty or whitespace-only.",
        )

    project_rows = _rows(
        await db.client.table("projects").select("id").eq("id", project_id).execute()
    )
    if not project_rows:
        raise HTTPException(status_code=404, detail="Project not found")
    db.project_id = project_id

    content = (request.content or "").strip()
    page = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        project_id=project_id,
        content=content or headline,
        headline=headline,
        abstract=content,
        provenance_model="human",
        extra={"status": "open"},
    )
    await db.save_page(page)
    try:
        await embed_and_store_page(db, page, field_name="abstract")
    except Exception:
        log.warning("Failed to embed new root question %s", page.id[:8], exc_info=True)

    log.info(
        "Root question created via API: project=%s id=%s headline=%s",
        project_id[:8],
        page.id[:8],
        headline[:70],
    )
    return page


@app.get(
    "/api/projects/{project_id}/calls",
    response_model=list[Call],
)
async def list_calls(
    project_id: str,
    question_id: str | None = None,
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
async def get_call(call_id: str, db: DB = Depends(_get_db)):
    call = await db.get_call(call_id)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")
    return call


@app.get("/api/calls/{call_id}/children", response_model=list[Call])
async def get_child_calls(call_id: str, db: DB = Depends(_get_db)):
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
async def get_run_trace_tree(run_id: str, db: DB = Depends(_get_db)):
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


@app.get("/api/runs/{run_id}/spend", response_model=RunSpendOut)
async def get_run_spend(run_id: str, db: DB = Depends(_get_db)):
    """Per-call-type spend breakdown for a run.

    Aggregates ``cost_usd`` and ``completed_at - created_at`` across the
    run's calls in Python — the row count per run is always small (tens,
    not thousands), so a GROUP BY trip round is unnecessary. Calls that
    haven't completed yet contribute 0 duration. Returned rows are sorted
    descending by ``cost_usd`` so the biggest spenders surface first.
    """
    run = await db.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    calls = await db.get_calls_for_run(run_id)

    totals_cost: dict[str, float] = {}
    totals_duration: dict[str, int] = {}
    counts: dict[str, int] = {}
    for c in calls:
        key = c.call_type.value
        counts[key] = counts.get(key, 0) + 1
        totals_cost[key] = totals_cost.get(key, 0.0) + (c.cost_usd or 0.0)
        if c.completed_at is not None:
            delta_ms = int((c.completed_at - c.created_at).total_seconds() * 1000)
            if delta_ms > 0:
                totals_duration[key] = totals_duration.get(key, 0) + delta_ms

    rows = [
        RunSpendByCallTypeOut(
            call_type=ct,
            count=counts[ct],
            cost_usd=round(totals_cost.get(ct, 0.0), 6),
            duration_ms=totals_duration.get(ct, 0),
        )
        for ct in counts
    ]
    rows.sort(key=lambda r: (-r.cost_usd, r.call_type))

    return RunSpendOut(
        run_id=run_id,
        run_id_short=run_id[:8],
        total_cost_usd=round(sum(r.cost_usd for r in rows), 6),
        total_duration_ms=sum(r.duration_ms for r in rows),
        total_calls=len(calls),
        by_call_type=rows,
    )


async def _run_background(label: str, coro):
    """Wrap a coroutine so unhandled exceptions are logged instead of vanishing.

    Background orchestrator runs can take minutes; if the server restarts
    mid-run the task is lost. This is a known limit of the fire-and-forget
    pattern — acceptable for operator-triggered kickoffs in the UI.
    """
    try:
        await coro
    except Exception:
        log.exception("Background task %s failed", label)


async def _run_continue_orchestrator(
    run_id: str,
    question_id: str,
    project_id: str,
    budget: int,
) -> None:
    """Spin up a fresh DB for the orchestrator and run to completion."""
    prod = get_settings().is_prod_db
    db = await DB.create(run_id=run_id, prod=prod, project_id=project_id)
    try:
        await db.init_budget(budget)
        question = await db.get_page(question_id)
        headline = question.headline if question else question_id[:8]
        await db.create_run(
            name=f"continue (api): {headline[:90]}",
            question_id=question_id,
            config=get_settings().capture_config(),
        )
        orch = Orchestrator(db)
        await orch.run(question_id)
    finally:
        await db.close()


async def _run_ab_eval_background(
    run_id_a: str,
    run_id_b: str,
    project_id: str,
) -> None:
    """Spin up a fresh DB for the AB eval and run to completion."""
    from rumil.ab_eval import run_ab_eval

    prod = get_settings().is_prod_db
    db = await DB.create(run_id=str(uuid.uuid4()), prod=prod, project_id=project_id)
    try:
        await run_ab_eval(run_id_a=run_id_a, run_id_b=run_id_b, db=db)
    finally:
        await db.close()


class ContinueQuestionIn(BaseModel):
    budget: int = 10


class ABEvalIn(BaseModel):
    run_id_a: str
    run_id_b: str


@app.post("/api/questions/{question_id}/continue", status_code=202)
async def post_continue_question(
    question_id: str,
    body: ContinueQuestionIn,
    db: DB = Depends(_get_db),
):
    """Fire a background orchestrator run on an existing question.

    Returns the new run_id immediately; client can navigate to
    /traces/{run_id} to watch the trace live. The server does not await
    the orchestrator — known risk: if the server restarts mid-run the
    task is lost.
    """
    question = await db.get_page(question_id)
    if not question:
        raise HTTPException(status_code=404, detail="Question not found")
    if question.page_type != PageType.QUESTION:
        raise HTTPException(
            status_code=400,
            detail=f"Page {question_id} is a {question.page_type.value}, not a question",
        )
    if body.budget < 1:
        raise HTTPException(status_code=400, detail="Budget must be >= 1")

    new_run_id = str(uuid.uuid4())
    project_id = question.project_id or ""

    task = asyncio.create_task(
        _run_background(
            f"continue question={question_id[:8]} run={new_run_id[:8]}",
            _run_continue_orchestrator(
                run_id=new_run_id,
                question_id=question_id,
                project_id=project_id,
                budget=body.budget,
            ),
        )
    )
    _track_background(task)

    return {"run_id": new_run_id, "question_id": question_id, "budget": body.budget}


@app.post("/api/runs/{run_id}/stage", status_code=200)
async def post_stage_run(run_id: str, db: DB = Depends(_get_db)):
    """Retroactively stage a completed non-staged run.

    Flips the run's rows to staged=true and reverts direct mutations from
    the event log so baseline readers see the pre-run state.
    """
    run = await db.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.get("staged"):
        raise HTTPException(status_code=409, detail="Run is already staged")
    await db.stage_run(run_id)
    return {"run_id": run_id, "staged": True}


@app.post("/api/runs/{run_id}/commit", status_code=200)
async def post_commit_run(run_id: str, db: DB = Depends(_get_db)):
    """Commit a staged run, making its effects visible to all readers."""
    run = await db.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if not run.get("staged"):
        raise HTTPException(status_code=409, detail="Run is not staged")
    await db.commit_staged_run(run_id)
    return {"run_id": run_id, "staged": False}


@app.get("/api/calls/{call_id}/events", response_model=list[TraceEventOut])
async def get_call_events(call_id: str, db: DB = Depends(_get_db)):
    call = await db.get_call(call_id)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")
    return await _parse_trace_events(db, call_id)


@app.get(
    "/api/ab-evals",
    response_model=list[ABEvalReportListItemOut],
)
async def list_ab_evals(db: DB = Depends(_get_db)):
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


@app.post("/api/ab-evals", status_code=202)
async def post_ab_eval(body: ABEvalIn, db: DB = Depends(_get_db)):
    """Kick off an AB eval comparing two runs as a background task.

    Returns an eval_run_id (the run_id used for the eval itself). The
    final ``ab_eval_report`` id is only known when the eval completes,
    so clients should poll /api/ab-evals and filter by run_id_a/b to
    find the finished report.
    """
    if body.run_id_a == body.run_id_b:
        raise HTTPException(status_code=400, detail="run_id_a and run_id_b must differ")

    run_a = await db.get_run(body.run_id_a)
    if not run_a:
        raise HTTPException(status_code=404, detail=f"Run {body.run_id_a} not found")
    run_b = await db.get_run(body.run_id_b)
    if not run_b:
        raise HTTPException(status_code=404, detail=f"Run {body.run_id_b} not found")

    project_id = run_a.get("project_id") or run_b.get("project_id") or ""

    task = asyncio.create_task(
        _run_background(
            f"ab_eval a={body.run_id_a[:8]} b={body.run_id_b[:8]}",
            _run_ab_eval_background(
                run_id_a=body.run_id_a,
                run_id_b=body.run_id_b,
                project_id=project_id,
            ),
        )
    )
    _track_background(task)
    return {
        "run_id_a": body.run_id_a,
        "run_id_b": body.run_id_b,
        "status": "started",
    }


@app.get(
    "/api/ab-evals/{eval_id}",
    response_model=ABEvalReportOut,
)
async def get_ab_eval(eval_id: str, db: DB = Depends(_get_db)):
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
                report_a=d.get("report_a", ""),
                report_b=d.get("report_b", ""),
                comparison=d.get("comparison", ""),
                call_id_a=d.get("call_id_a", ""),
                call_id_b=d.get("call_id_b", ""),
                comparison_call_id=d.get("comparison_call_id", ""),
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
async def list_llm_exchanges(call_id: str, db: DB = Depends(_get_db)):
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
async def get_llm_exchange(exchange_id: str, db: DB = Depends(_get_db)):
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
async def get_page_run(page_id: str, db: DB = Depends(_get_db)):
    run = await db.get_run_for_page(page_id)
    if not run:
        return None
    return RunSummaryOut(
        run_id=run["run_id"],
        created_at=run["created_at"],
        provenance_call_id=run.get("provenance_call_id", ""),
    )


@app.get("/api/projects/{project_id}/suggestions")
async def list_suggestions(
    project_id: str,
    status: str = "pending",
    target_page_id: str | None = None,
    db: DB = Depends(_get_db),
):
    suggestions = await db.get_suggestions(
        status=status,
        target_page_id=target_page_id,
    )
    page_ids = list({s.target_page_id for s in suggestions if s.target_page_id})
    pages = await db.get_pages_by_ids(page_ids) if page_ids else {}
    result = []
    for s in suggestions:
        d = s.model_dump(mode="json")
        page = pages.get(s.target_page_id)
        d["target_headline"] = page.headline if page else None
        result.append(d)
    return result


@app.post("/api/suggestions/{suggestion_id}/review")
async def review_suggestion(
    suggestion_id: str,
    status: str = "accepted",
    db: DB = Depends(_get_db),
):
    if status not in ("accepted", "rejected", "dismissed"):
        raise HTTPException(400, f"Invalid status: {status}")
    if status != "accepted":
        await db.update_suggestion_status(suggestion_id, SuggestionStatus(status))
        return {"ok": True, "status": status}
    suggestion = await db.get_suggestion(suggestion_id)
    if not suggestion:
        raise HTTPException(404, "Suggestion not found")
    result = await _apply_suggestion(db, suggestion)
    await db.update_suggestion_status(suggestion_id, SuggestionStatus.ACCEPTED)
    return {"ok": True, "status": "accepted", **result}


async def _apply_suggestion(db: DB, suggestion: Suggestion) -> dict:
    """Execute the mutation described by a suggestion."""
    stype = suggestion.suggestion_type
    payload = suggestion.payload
    target = suggestion.target_page_id

    if stype == SuggestionType.RELEVEL:
        new_importance = payload.get("new_importance")
        if new_importance is None:
            raise HTTPException(400, "RELEVEL suggestion missing new_importance")
        await db.update_page_importance(target, int(new_importance))
        return {"action": "relevel", "page_id": target, "new_importance": new_importance}

    if stype == SuggestionType.MERGE_DUPLICATE:
        keep_id = payload.get("keep_node_id") or payload.get("keep_page_id")
        supersede_id = payload.get("supersede_node_id") or payload.get("supersede_page_id")
        if not keep_id or not supersede_id:
            raise HTTPException(400, "MERGE_DUPLICATE suggestion missing keep/supersede IDs")
        await db.supersede_page(str(supersede_id), str(keep_id))
        return {"action": "merge_duplicate", "kept": keep_id, "superseded": supersede_id}

    if stype == SuggestionType.RESOLVE_TENSION:
        other_id = payload.get("other_node_id") or payload.get("other_page_id")
        reasoning = payload.get("reasoning", "")
        if not other_id:
            raise HTTPException(400, "RESOLVE_TENSION suggestion missing other_node_id")
        link = PageLink(
            from_page_id=target,
            to_page_id=str(other_id),
            link_type=LinkType.RELATED,
            reasoning=str(reasoning),
        )
        await db.save_link(link)
        return {"action": "resolve_tension", "link_id": link.id}

    if stype == SuggestionType.CASCADE_REVIEW:
        return {"action": "cascade_review", "acknowledged": True}

    if stype == SuggestionType.AUTO_INVESTIGATE:
        return {"action": "auto_investigate", "deferred": True}

    return {"action": "unknown"}


@app.get("/api/questions/{question_id}/view")
async def get_question_view(
    question_id: str,
    importance_threshold: int = 3,
    db: DB = Depends(_get_db),
):
    resolved = await db.resolve_page_id(question_id)
    if not resolved:
        raise HTTPException(404, f"Question {question_id} not found")
    page = await db.get_page(resolved)
    if not page or page.page_type != PageType.QUESTION:
        raise HTTPException(
            404,
            f"Page {question_id} is not a question page; cannot build a view",
        )
    view = await build_view(db, resolved, importance_threshold=importance_threshold)
    return {
        "question": view.question,
        "sections": [
            {
                "name": s.name,
                "description": s.description,
                "items": [
                    {
                        "page": item.page,
                        "links": item.links,
                        "section": item.section,
                    }
                    for item in s.items
                ],
            }
            for s in view.sections
        ],
        "health": {
            "total_pages": view.health.total_pages,
            "missing_credence": view.health.missing_credence,
            "missing_importance": view.health.missing_importance,
            "child_questions_without_judgements": view.health.child_questions_without_judgements,
            "max_depth": view.health.max_depth,
        },
    }


@app.get("/api/config", response_model=AppConfigOut)
async def get_app_config():
    """Expose feature flags needed by the frontend.

    The friendly-user view-reading UI uses this to decide whether to show
    flag affordances. enable_flag_issue=False means flagging is disabled
    server-side; the frontend should hide the UI.
    """
    settings = get_settings()
    return AppConfigOut(enable_flag_issue=settings.enable_flag_issue)


@app.post(
    "/api/view-items/{item_id}/flag",
    response_model=ViewItemFlagOut,
)
async def flag_view_item(
    item_id: str,
    request: ViewItemFlagRequest,
    db: DB = Depends(_get_db),
):
    """Record a friendly-user flag on a specific view item (page).

    Friendly-user surface for the View reader. Accepts a short or full
    page id. Writes a page_flags row with flag_type='view_item_issue'
    and records a human_feedback reputation event mirroring the
    flag_funniness hook.

    Gated by settings.enable_flag_issue: returns 403 if disabled.
    """
    settings = get_settings()
    if not settings.enable_flag_issue:
        raise HTTPException(
            status_code=403,
            detail="Flagging is currently disabled (enable_flag_issue=False).",
        )

    resolved = await db.resolve_page_id(item_id)
    if not resolved:
        raise HTTPException(status_code=404, detail=f"View item {item_id} not found")

    page = await db.get_page(resolved)
    if page is None:
        raise HTTPException(status_code=404, detail=f"View item {item_id} not found")
    if page.project_id and not db.project_id:
        db.project_id = page.project_id

    assert db.project_id, "project_id must be set before recording friendly-user events"
    db.run_id = await db.get_or_create_named_run(
        project_id=db.project_id,
        name="friendly-user-feedback",
        config={"origin": "friendly-user-feedback"},
    )

    note = f"[{request.category}] {request.message}"
    if request.suggested_fix:
        note += f"\n\nSuggested fix: {request.suggested_fix}"

    flag_id = str(uuid.uuid4())
    await db._execute(
        db.client.table("page_flags").insert(
            {
                "id": flag_id,
                "flag_type": "view_item_issue",
                "page_id": resolved,
                "call_id": None,
                "page_id_a": None,
                "page_id_b": None,
                "note": note,
                "created_at": datetime.now(UTC).isoformat(),
                "run_id": db.run_id,
                "staged": db.staged,
            }
        ),
    )

    subject_run_id = page.run_id if page is not None else ""
    orchestrator: str | None = None
    if subject_run_id:
        run_row = await db.get_run(subject_run_id)
        if run_row:
            config = run_row.get("config") or {}
            if isinstance(config, dict):
                val = config.get("orchestrator")
                orchestrator = val if isinstance(val, str) else None

    await db.record_reputation_event(
        source="human_feedback",
        dimension="view_item_issue",
        score=1.0,
        orchestrator=orchestrator,
        extra={
            "subject_run_id": subject_run_id,
            "flagged_page_id": resolved,
            "category": request.category,
        },
    )

    log.info(
        "View item flagged: page=%s, category=%s, message=%s",
        resolved[:8],
        request.category,
        request.message[:80],
    )
    return ViewItemFlagOut(ok=True, flag_id=flag_id, page_id=resolved)


@app.delete(
    "/api/view-items/flags/{flag_id}",
    response_model=ViewItemFlagDeleteOut,
)
async def undo_view_item_flag(
    flag_id: str,
    db: DB = Depends(_get_db),
):
    """Undo a just-submitted flag within the friendly-user grace window.

    Deletes the page_flags row AND the mirrored reputation_events row
    written by the flag endpoint. Idempotent: a missing row is a no-op.

    Still gated by enable_flag_issue — if flagging is server-disabled,
    undo is blocked too (no reason to allow mutation when flagging is off).
    """
    settings = get_settings()
    if not settings.enable_flag_issue:
        raise HTTPException(
            status_code=403,
            detail="Flagging is currently disabled (enable_flag_issue=False).",
        )

    flag_rows = _rows(
        await db._execute(db.client.table("page_flags").select("*").eq("id", flag_id))
    )
    if not flag_rows:
        return ViewItemFlagDeleteOut(ok=True, flag_id=flag_id)

    flag_row = flag_rows[0]
    if flag_row.get("flag_type") != "view_item_issue":
        raise HTTPException(
            status_code=400,
            detail="Flag is not a view_item_issue and cannot be undone here.",
        )

    flag_run_id = flag_row.get("run_id")
    page_id = flag_row.get("page_id")

    await db._execute(db.client.table("page_flags").delete().eq("id", flag_id))

    if flag_run_id and page_id:
        # Narrow by flagged_page_id in extra — since the flag endpoint now
        # shares one run row per project for friendly-user telemetry, an
        # unfiltered delete would remove every view_item_issue event in the
        # project. Scope to this specific flag's page instead.
        await db._execute(
            db.client.table("reputation_events")
            .delete()
            .eq("run_id", flag_run_id)
            .eq("source", "human_feedback")
            .eq("dimension", "view_item_issue")
            .contains("extra", {"flagged_page_id": page_id})
        )
        log.info("View item flag undone: flag=%s page=%s", flag_id[:8], page_id[:8])

    return ViewItemFlagDeleteOut(ok=True, flag_id=flag_id)


@app.post(
    "/api/view-items/{item_id}/read",
    response_model=ViewItemReadOut,
)
async def record_view_item_read(
    item_id: str,
    request: ViewItemReadRequest,
    db: DB = Depends(_get_db),
):
    """Record that a friendly user actually read a view item.

    Endogenous read-side signal: on a ~2s dwell the frontend POSTs here and
    we write a reputation_events row with source=human_feedback,
    dimension=read_time, score=1.0, extra={subject_page_id, seconds}.

    The frontend deduplicates per-item per-session, so one event per
    (user_session, page) is the expected cardinality.
    """
    resolved = await db.resolve_page_id(item_id)
    if not resolved:
        raise HTTPException(status_code=404, detail=f"View item {item_id} not found")

    page = await db.get_page(resolved)
    if page is None:
        raise HTTPException(status_code=404, detail=f"View item {item_id} not found")
    if page.project_id and not db.project_id:
        db.project_id = page.project_id

    assert db.project_id, "project_id must be set before recording friendly-user events"
    db.run_id = await db.get_or_create_named_run(
        project_id=db.project_id,
        name="friendly-user-feedback",
        config={"origin": "friendly-user-feedback"},
    )

    subject_run_id = page.run_id or ""
    orchestrator: str | None = None
    if subject_run_id:
        run_row = await db.get_run(subject_run_id)
        if run_row:
            config = run_row.get("config") or {}
            if isinstance(config, dict):
                val = config.get("orchestrator")
                orchestrator = val if isinstance(val, str) else None

    seconds = max(0.0, float(request.seconds))
    await db.record_reputation_event(
        source="human_feedback",
        dimension="read_time",
        score=1.0,
        orchestrator=orchestrator,
        extra={
            "subject_run_id": subject_run_id,
            "subject_page_id": resolved,
            "seconds": seconds,
        },
    )

    return ViewItemReadOut(ok=True, page_id=resolved)


@app.post("/api/annotations", response_model=AnnotationCreateOut)
async def create_annotation(
    request: AnnotationCreateRequest,
    http_request: Request,
    db: DB = Depends(_get_db),
):
    """Record a human-authored annotation.

    General-purpose annotation surface (doc 28 MVP). Accepts any of the four
    MVP annotation types (``span``, ``counterfactual_tool_use``, ``flag``,
    ``endorsement``) and writes a row with ``author_type='human'``.

    This endpoint is allowed for the friendly-user password as well as the
    admin password — annotation is the whole point of letting friendly users
    weigh in. ``author_id`` is derived from the request (client IP for now;
    swap for session id once that exists).

    Also records a mirrored reputation_events row (``source='human_feedback'``,
    ``dimension=<annotation_type>``, ``score=1.0``) behind a non-fatal try
    so a transient reputation-events failure doesn't drop the annotation.
    """
    project_id: str | None = None
    if request.target_page_id:
        resolved_page = await db.resolve_page_id(request.target_page_id) or request.target_page_id
        page = await db.get_page(resolved_page)
        if page is None:
            raise HTTPException(
                status_code=404, detail=f"target_page_id {request.target_page_id} not found"
            )
        target_page = resolved_page
        project_id = page.project_id
    else:
        target_page = None

    target_call = request.target_call_id
    if target_call:
        call_rows = _rows(
            await db.client.table("calls").select("project_id").eq("id", target_call).execute()
        )
        if not call_rows:
            raise HTTPException(status_code=404, detail=f"target_call_id {target_call} not found")
        project_id = project_id or call_rows[0].get("project_id")

    if project_id and not db.project_id:
        db.project_id = project_id

    await db.create_run(
        name="human-annotation",
        question_id=None,
        config={"origin": "human-annotation"},
    )

    client_host = http_request.client.host if http_request.client else "unknown"
    ev = await db.record_annotation(
        annotation_type=request.annotation_type,
        author_type="human",
        author_id=f"http:{client_host}",
        target_page_id=target_page,
        target_call_id=target_call,
        target_event_seq=request.target_event_seq,
        span_start=request.span_start,
        span_end=request.span_end,
        category=request.category,
        note=request.note,
        payload=request.payload,
        extra=request.extra,
    )

    try:
        await db.record_reputation_event(
            source="human_feedback",
            dimension=request.annotation_type,
            score=1.0,
            source_call_id=target_call,
            extra={
                "annotation_id": ev.id,
                "target_page_id": target_page,
                "target_call_id": target_call,
                "category": request.category,
            },
        )
    except Exception:
        log.exception("Failed to mirror annotation into reputation_events (non-fatal)")

    log.info(
        "Annotation recorded: type=%s target_page=%s target_call=%s",
        request.annotation_type,
        (target_page or "-")[:8],
        (target_call or "-")[:8],
    )
    return AnnotationCreateOut(ok=True, annotation_id=ev.id)


@app.get("/api/pages/{page_id}/annotations", response_model=list[AnnotationEvent])
async def list_page_annotations(
    page_id: str,
    db: DB = Depends(_get_db),
):
    """List annotations targeting a given page."""
    resolved = await db.resolve_page_id(page_id) or page_id
    page = await db.get_page(resolved)
    if page is None:
        raise HTTPException(status_code=404, detail=f"Page {page_id} not found")
    if page.project_id and not db.project_id:
        db.project_id = page.project_id
    return await db.get_annotations(target_page_id=resolved)


@app.get("/api/calls/{call_id}/annotations", response_model=list[AnnotationEvent])
async def list_call_annotations(
    call_id: str,
    db: DB = Depends(_get_db),
):
    """List annotations targeting a given call."""
    call_rows = _rows(
        await db.client.table("calls").select("project_id").eq("id", call_id).execute()
    )
    if not call_rows:
        raise HTTPException(status_code=404, detail=f"Call {call_id} not found")
    project_id = call_rows[0].get("project_id")
    if project_id and not db.project_id:
        db.project_id = project_id
    return await db.get_annotations(target_call_id=call_id)


@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    return await handle_chat(request)


@app.post("/api/chat/stream")
async def chat_stream(request: ChatRequest):
    return await handle_chat_stream(request)


@app.get(
    "/api/chat/conversations",
    response_model=list[ConversationListItem],
)
async def list_chat_conversations(
    project_id: str,
    question_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: DB = Depends(_get_db),
):
    conversations = await db.list_chat_conversations(
        project_id=project_id,
        question_id=question_id,
        limit=limit,
        offset=offset,
    )
    return [
        ConversationListItem(
            id=c.id,
            project_id=c.project_id,
            question_id=c.question_id,
            title=c.title,
            created_at=c.created_at.isoformat(),
            updated_at=c.updated_at.isoformat(),
            parent_conversation_id=c.parent_conversation_id,
            branched_at_seq=c.branched_at_seq,
        )
        for c in conversations
    ]


@app.get(
    "/api/chat/conversations/{conversation_id}",
    response_model=ConversationDetail,
)
async def get_chat_conversation(
    conversation_id: str,
    db: DB = Depends(_get_db),
):
    conv = await db.get_chat_conversation(conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    messages = await db.list_chat_messages(conversation_id)
    return ConversationDetail(
        id=conv.id,
        project_id=conv.project_id,
        question_id=conv.question_id,
        title=conv.title,
        created_at=conv.created_at.isoformat(),
        updated_at=conv.updated_at.isoformat(),
        messages=[
            {
                "id": m.id,
                "role": m.role.value,
                "content": m.content,
                "seq": m.seq,
                "ts": m.ts.isoformat(),
                "question_id": m.question_id,
            }
            for m in messages
        ],
        parent_conversation_id=conv.parent_conversation_id,
        branched_at_seq=conv.branched_at_seq,
    )


@app.post(
    "/api/chat/conversations",
    response_model=ConversationListItem,
)
async def create_chat_conversation(
    request: CreateConversationRequest,
    db: DB = Depends(_get_db),
):
    title = request.title
    if not title and request.first_message:
        title = _derive_title(request.first_message)
    if not title:
        title = "(new conversation)"
    conv = await db.create_chat_conversation(
        project_id=request.project_id,
        question_id=request.question_id,
        title=title,
    )
    if request.first_message:
        await db.save_chat_message(
            conversation_id=conv.id,
            role=ChatMessageRole.USER,
            content={"text": request.first_message},
            question_id=request.question_id,
        )
    return ConversationListItem(
        id=conv.id,
        project_id=conv.project_id,
        question_id=conv.question_id,
        title=conv.title,
        created_at=conv.created_at.isoformat(),
        updated_at=conv.updated_at.isoformat(),
        parent_conversation_id=conv.parent_conversation_id,
        branched_at_seq=conv.branched_at_seq,
    )


@app.patch(
    "/api/chat/conversations/{conversation_id}",
    response_model=ConversationListItem,
)
async def update_chat_conversation(
    conversation_id: str,
    request: UpdateConversationRequest,
    db: DB = Depends(_get_db),
):
    existing = await db.get_chat_conversation(conversation_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Conversation not found")
    await db.update_chat_conversation(conversation_id, title=request.title)
    refreshed = await db.get_chat_conversation(conversation_id)
    assert refreshed is not None
    return ConversationListItem(
        id=refreshed.id,
        project_id=refreshed.project_id,
        question_id=refreshed.question_id,
        title=refreshed.title,
        created_at=refreshed.created_at.isoformat(),
        updated_at=refreshed.updated_at.isoformat(),
        parent_conversation_id=refreshed.parent_conversation_id,
        branched_at_seq=refreshed.branched_at_seq,
    )


@app.delete("/api/chat/conversations/{conversation_id}")
async def delete_chat_conversation(
    conversation_id: str,
    db: DB = Depends(_get_db),
):
    existing = await db.get_chat_conversation(conversation_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Conversation not found")
    await db.soft_delete_chat_conversation(conversation_id)
    return {"ok": True, "id": conversation_id}


@app.post(
    "/api/chat/conversations/{conversation_id}/branch",
    response_model=ConversationDetail,
)
async def branch_chat_conversation(
    conversation_id: str,
    request: BranchConversationRequest,
    db: DB = Depends(_get_db),
):
    """Fork a conversation at message `at_seq` into a new one.

    Copies every message with seq <= at_seq from the source into a new
    conversation, tags the new conversation with parent_conversation_id +
    branched_at_seq, and returns the new conversation (detail-shaped, so
    the frontend has the copied messages ready without a second round trip).
    The parent conversation is NOT modified — branching is additive.
    """
    existing = await db.get_chat_conversation(conversation_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Conversation not found")
    try:
        new_conv = await db.branch_chat_conversation(
            source_conversation_id=conversation_id,
            at_seq=request.at_seq,
            title=request.title,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    messages = await db.list_chat_messages(new_conv.id)
    return ConversationDetail(
        id=new_conv.id,
        project_id=new_conv.project_id,
        question_id=new_conv.question_id,
        title=new_conv.title,
        created_at=new_conv.created_at.isoformat(),
        updated_at=new_conv.updated_at.isoformat(),
        messages=[
            {
                "id": m.id,
                "role": m.role.value,
                "content": m.content,
                "seq": m.seq,
                "ts": m.ts.isoformat(),
                "question_id": m.question_id,
            }
            for m in messages
        ],
        parent_conversation_id=new_conv.parent_conversation_id,
        branched_at_seq=new_conv.branched_at_seq,
    )


@app.get(
    "/api/runs/{run_id}/page-load-stats",
    response_model=PageLoadStatsOut,
)
async def get_page_load_stats(run_id: str, db: DB = Depends(_get_db)):
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

    return PageLoadStatsOut(
        events=events,
        total=len(rows),
        total_unique=len(unique_pages),
    )


@app.get(
    "/api/projects/{project_id}/reputation",
    response_model=ReputationSummaryOut,
)
async def get_reputation_summary(
    project_id: str,
    orchestrator: str | None = None,
    source: str | None = None,
    dimension: str | None = None,
    db: DB = Depends(_get_db),
):
    """Grouped reputation summary for a project.

    Buckets are grouped by (source, dimension, orchestrator). Each row is a
    non-collapsed summary so eval_agent and human_feedback signals always
    surface separately. See marketplace-thread/13-reputation-governance.md.
    """
    buckets = await db.get_reputation_summary(
        project_id,
        orchestrator=orchestrator,
        source=source,
        dimension=dimension,
    )
    return ReputationSummaryOut(
        project_id=project_id,
        total_events=sum(b["n_events"] for b in buckets),
        buckets=[ReputationBucketOut(**b) for b in buckets],
    )


@app.get(
    "/api/projects/{project_id}/reputation/events",
    response_model=list[ReputationEvent],
)
async def list_reputation_events(
    project_id: str,
    orchestrator: str | None = None,
    source: str | None = None,
    dimension: str | None = None,
    limit: int = 100,
    db: DB = Depends(_get_db),
):
    """Recent raw reputation events for a project (newest first).

    The DB helper already applies staged-visibility + project scoping.
    Limit is clamped to [1, 500].
    """
    limit = max(1, min(limit, 500))
    events = await db.get_reputation_events(
        source=source,
        dimension=dimension,
        orchestrator=orchestrator,
    )
    events.sort(key=lambda e: e.created_at, reverse=True)
    return events[:limit]
