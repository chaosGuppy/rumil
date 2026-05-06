"""Atlas API: parallel set of /api/atlas routes that expose the atlas
self-describing registry, workflow profiles, and aggregate-behavior
rollups. Read-only.

The atlas reads live from the same registries the LLM consumes — moves,
dispatch defs, available-moves / available-calls presets, prompt
markdown, plus a thin canonical-descriptions layer for enums. Drift
between runtime behaviour and the rendered docs is impossible by
construction for everything except the workflow stage spec; that one
piece has a description-completeness test.

Routes:

- ``GET /api/atlas/registry`` — top-level rollup (counts + summaries).
- ``GET /api/atlas/registry/moves`` / ``/{move_type}``
- ``GET /api/atlas/registry/dispatches`` / ``/{call_type}``
- ``GET /api/atlas/registry/calls`` / ``/{call_type}``
- ``GET /api/atlas/registry/pages`` / ``/{page_type}``
- ``GET /api/atlas/registry/prompts`` / ``/{name}``
- ``GET /api/atlas/workflows`` / ``/{name}``
- ``GET /api/atlas/workflows/{name}/aggregate`` (project_id optional)
- ``GET /api/atlas/runs/{run_id}/flow``
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException

from rumil.api.auth import AuthUser, get_current_user, require_admin
from rumil.atlas.aggregate import build_run_flow, build_workflow_aggregate
from rumil.atlas.gaps import build_gaps_report
from rumil.atlas.graph import build_workflow_graph
from rumil.atlas.overlay import build_workflow_overlay
from rumil.atlas.prompt_parts import build_prompt_composition
from rumil.atlas.registry import (
    build_call_type_summaries,
    build_dispatch_summaries,
    build_move_summaries,
    build_page_type_summaries,
    build_registry_rollup,
    get_prompt_doc,
    list_prompt_files,
)
from rumil.atlas.schemas import (
    CallTypeStats,
    CallTypeSummary,
    DispatchSummary,
    GapsReport,
    MoveStats,
    MoveSummary,
    PageTypeSummary,
    PromptComposition,
    PromptDoc,
    RegistryRollup,
    RunFlow,
    SearchResults,
    WorkflowAggregate,
    WorkflowGraph,
    WorkflowOverlay,
    WorkflowProfile,
    WorkflowSummary,
)
from rumil.atlas.search import search_atlas
from rumil.atlas.stats import build_call_type_stats, build_move_stats
from rumil.atlas.workflows import get_workflow_profile, list_workflow_summaries
from rumil.database import DB
from rumil.settings import get_settings


async def _get_db(
    project_id: str = "",
    _user: AuthUser = Depends(get_current_user),
) -> AsyncIterator[DB]:
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


router = APIRouter(
    prefix="/api/atlas",
    tags=["atlas"],
    dependencies=[Depends(require_admin)],
)


@router.get("/registry", response_model=RegistryRollup)
def get_registry_rollup(
    _user: AuthUser = Depends(get_current_user),
) -> RegistryRollup:
    return build_registry_rollup(list_workflow_summaries())


@router.get("/registry/moves", response_model=list[MoveSummary])
def list_moves(
    _user: AuthUser = Depends(get_current_user),
) -> list[MoveSummary]:
    return build_move_summaries()


@router.get("/registry/moves/{move_type}", response_model=MoveSummary)
def get_move(
    move_type: str,
    _user: AuthUser = Depends(get_current_user),
) -> MoveSummary:
    for m in build_move_summaries():
        if m.move_type == move_type or m.name == move_type:
            return m
    raise HTTPException(status_code=404, detail=f"move not found: {move_type}")


@router.get("/registry/dispatches", response_model=list[DispatchSummary])
def list_dispatches(
    _user: AuthUser = Depends(get_current_user),
) -> list[DispatchSummary]:
    return build_dispatch_summaries()


@router.get("/registry/dispatches/{call_type}", response_model=DispatchSummary)
def get_dispatch(
    call_type: str,
    _user: AuthUser = Depends(get_current_user),
) -> DispatchSummary:
    for d in build_dispatch_summaries():
        if d.call_type == call_type or d.name == call_type:
            return d
    raise HTTPException(status_code=404, detail=f"dispatch not found: {call_type}")


@router.get("/registry/calls", response_model=list[CallTypeSummary])
def list_call_types(
    _user: AuthUser = Depends(get_current_user),
) -> list[CallTypeSummary]:
    return build_call_type_summaries()


@router.get("/registry/calls/{call_type}", response_model=CallTypeSummary)
def get_call_type(
    call_type: str,
    _user: AuthUser = Depends(get_current_user),
) -> CallTypeSummary:
    for c in build_call_type_summaries():
        if c.call_type == call_type:
            return c
    raise HTTPException(status_code=404, detail=f"call type not found: {call_type}")


@router.get("/registry/pages", response_model=list[PageTypeSummary])
def list_page_types(
    _user: AuthUser = Depends(get_current_user),
) -> list[PageTypeSummary]:
    return build_page_type_summaries()


@router.get("/registry/pages/{page_type}", response_model=PageTypeSummary)
def get_page_type(
    page_type: str,
    _user: AuthUser = Depends(get_current_user),
) -> PageTypeSummary:
    for p in build_page_type_summaries():
        if p.page_type == page_type:
            return p
    raise HTTPException(status_code=404, detail=f"page type not found: {page_type}")


@router.get("/registry/compositions/{key}", response_model=PromptComposition)
def get_composition(
    key: str,
    _user: AuthUser = Depends(get_current_user),
) -> PromptComposition:
    comp = build_prompt_composition(key)
    if not comp.parts:
        raise HTTPException(status_code=404, detail=f"composition not found: {key}")
    return comp


@router.get("/registry/prompts", response_model=list[str])
def list_prompts(
    _user: AuthUser = Depends(get_current_user),
) -> list[str]:
    return list_prompt_files()


@router.get("/registry/prompts/{name}", response_model=PromptDoc)
def get_prompt(
    name: str,
    _user: AuthUser = Depends(get_current_user),
) -> PromptDoc:
    doc = get_prompt_doc(name)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"prompt not found: {name}")
    return doc


@router.get("/workflows", response_model=list[WorkflowSummary])
def list_workflows(
    _user: AuthUser = Depends(get_current_user),
) -> list[WorkflowSummary]:
    return list_workflow_summaries()


@router.get("/workflows/graph", response_model=WorkflowGraph)
def get_workflow_graph_endpoint(
    _user: AuthUser = Depends(get_current_user),
) -> WorkflowGraph:
    return build_workflow_graph()


@router.get("/workflows/{name}", response_model=WorkflowProfile)
def get_workflow(
    name: str,
    _user: AuthUser = Depends(get_current_user),
) -> WorkflowProfile:
    profile = get_workflow_profile(name)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"workflow not found: {name}")
    return profile


@router.get("/workflows/{name}/aggregate", response_model=WorkflowAggregate)
async def get_workflow_aggregate(
    name: str,
    project_id: str | None = None,
    limit: int = 50,
    db: DB = Depends(_get_db),
) -> WorkflowAggregate:
    if get_workflow_profile(name) is None:
        raise HTTPException(status_code=404, detail=f"workflow not found: {name}")
    return await build_workflow_aggregate(db, name, project_id=project_id, limit=limit)


@router.get("/runs/{run_id}/flow", response_model=RunFlow)
async def get_run_flow_endpoint(
    run_id: str,
    db: DB = Depends(_get_db),
) -> RunFlow:
    return await build_run_flow(db, run_id)


@router.get("/calls/{call_type}/stats", response_model=CallTypeStats)
async def get_call_type_stats(
    call_type: str,
    project_id: str | None = None,
    n_runs: int = 50,
    db: DB = Depends(_get_db),
) -> CallTypeStats:
    from rumil.models import CallType

    try:
        ct = CallType(call_type)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"call type not found: {call_type}") from exc
    return await build_call_type_stats(db, ct, project_id=project_id, n_runs=n_runs)


@router.get("/moves/{move_type}/stats", response_model=MoveStats)
async def get_move_stats(
    move_type: str,
    project_id: str | None = None,
    n_runs: int = 50,
    db: DB = Depends(_get_db),
) -> MoveStats:
    return await build_move_stats(db, move_type, project_id=project_id, n_runs=n_runs)


@router.get("/gaps", response_model=GapsReport)
def get_gaps(
    _user: AuthUser = Depends(get_current_user),
) -> GapsReport:
    return build_gaps_report()


@router.get("/search", response_model=SearchResults)
def get_search(
    q: str = "",
    limit: int = 50,
    _user: AuthUser = Depends(get_current_user),
) -> SearchResults:
    return search_atlas(q, limit=limit)


@router.get("/workflows/{name}/runs/{run_id}/overlay", response_model=WorkflowOverlay)
async def get_workflow_overlay_endpoint(
    name: str,
    run_id: str,
    db: DB = Depends(_get_db),
) -> WorkflowOverlay:
    overlay = await build_workflow_overlay(db, name, run_id)
    if overlay is None:
        raise HTTPException(status_code=404, detail=f"workflow not found: {name}")
    return overlay
