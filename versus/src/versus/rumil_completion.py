"""Orchestrator-driven essay completions via the versus runner.

The single-shot completion path (one LLM call per essay × prefix ×
model) lives in :mod:`versus.complete`. This module adds the orch
path: each essay × prefix × workflow runs through
:func:`rumil.versus_runner.run_versus`, producing a continuation that
lands as a ``versus_texts`` row tagged
``source_id="orch:<workflow>:<model>:c<hash8>"``.

Source ID convention (decided on issue #426 comment 4361717743):
``orch:<workflow_name>:<model>:c<hash8>`` where ``<hash8>`` is the
first 8 hex chars of the run's ``config_hash`` (computed via
:func:`versus.versus_config.make_versus_config`). Pinning the
config_hash into the source_id means budget=4 and budget=10 of the
same workflow are separate contestants and can be paired against each
other in judging; different workflows under the same model are also
separate contestants.

Workflow registry: :data:`WORKFLOW_REGISTRY` maps the CLI's workflow
name to ``(workflow_class, default_kwargs)`` pairs. Today only
``two_phase`` is registered; ``draft_and_edit`` slots in once #427
lands.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from rumil.orchestrators.draft_and_edit import DraftAndEditWorkflow
from rumil.versus_workflow import TwoPhaseWorkflow
from versus import config, prepare, versus_db
from versus.run_summary import RunSummary
from versus.tasks import CompleteEssayTask, EssayPrefixContext

# Maps the CLI ``--orch <name>`` value to ``(workflow_class, default_kwargs)``.
# default_kwargs are merged with caller-supplied kwargs (e.g. ``budget``)
# at instantiation time. Adding a new workflow:
#   1. Implement the Workflow protocol (see rumil.versus_workflow).
#   2. Register it here with sensible defaults.
#   3. Update the run_completions.py docs and the rumil-versus-complete
#      skill.
#
# ``produces_artifact=True`` workflows (e.g. ``draft_and_edit``) are
# handled transparently by run_versus — the runner skips the closer
# call and reads ``question.content`` verbatim. No special wiring here.
WORKFLOW_REGISTRY: dict[str, tuple[type, dict[str, Any]]] = {
    "two_phase": (TwoPhaseWorkflow, {}),
    "draft_and_edit": (DraftAndEditWorkflow, {}),
}


@dataclass
class _PendingCompletion:
    """One planned (essay × prefix × workflow × model) row to produce."""

    task: prepare.PreparedTask
    source_id: str
    config_hash: str
    base_config: dict[str, Any]


def build_source_id(workflow_name: str, model: str, config_hash: str) -> str:
    """Compose the canonical ``versus_texts.source_id`` for an orch row.

    Format: ``orch:<workflow_name>:<model>:c<hash8>``. The ``c<hash8>``
    suffix lets two configs of the same workflow (e.g. budget=4 vs
    budget=10) coexist as separate contestants without colliding on
    ``source_id``. Single-shot completions use the bare model id, so
    the ``orch:`` prefix unambiguously distinguishes orch rows.
    """
    return f"orch:{workflow_name}:{model}:c{config_hash[:8]}"


def _make_workflow_and_task(
    workflow_name: str,
    *,
    budget: int,
) -> tuple[Any, CompleteEssayTask]:
    """Instantiate the workflow + task pair for a given run.

    Pulls the registered workflow class + defaults from
    :data:`WORKFLOW_REGISTRY` and merges the runtime ``budget`` in.
    Raises ``KeyError`` (with a list of registered names) if
    ``workflow_name`` isn't registered.
    """
    if workflow_name not in WORKFLOW_REGISTRY:
        valid = sorted(WORKFLOW_REGISTRY.keys())
        raise KeyError(f"unknown workflow {workflow_name!r}; registered: {valid}")
    cls, defaults = WORKFLOW_REGISTRY[workflow_name]
    kwargs: dict[str, Any] = {**defaults, "budget": budget}
    workflow = cls(**kwargs)
    task = CompleteEssayTask()
    return workflow, task


def _existing_source_ids(client, *, source_id: str, prefix_hash: str) -> set[str]:
    """Return the set of essay_ids that already have a row at this exact
    ``source_id`` × ``prefix_hash``.

    Used to skip planning for (essay, source_id) keys we'd dedup
    against. The full DB-side dedup primitive is ``request_hash``, but
    we don't construct a provider-shaped request body here — the orch
    path's "request" is the workflow + closer composition, captured
    structurally by ``base_config`` and recorded in ``params``. So we
    skip at the ``(essay_id, source_id, prefix_hash)`` granularity:
    one orch contestant per essay × prefix.
    """
    out: set[str] = set()
    for r in versus_db.iter_texts(client, kind="completion", light=True):
        if r["source_id"] != source_id:
            continue
        if r["prefix_hash"] != prefix_hash:
            continue
        out.add(r["essay_id"])
    return out


async def _resolve_workspace(db: Any, name: str) -> Any:
    """Look up a workspace by name, failing loudly if it doesn't exist.

    Mirrors :func:`versus.rumil_judge._resolve_workspace`; deliberately
    not factored into a shared helper so a future divergence (e.g. orch
    completions defaulting to a different workspace) doesn't have to
    fight a single helper.
    """
    projects = await db.list_projects(include_hidden=True)
    by_name = {p.name: p for p in projects}
    if name not in by_name:
        known = ", ".join(sorted(by_name.keys())) or "<none>"
        raise SystemExit(
            f"[err ] workspace {name!r} not found. "
            f"Create it via rumil's main.py, or pick from: {known}"
        )
    return by_name[name]


def _plan_pending(
    cfg: config.Config,
    essays: Sequence[Any],
    *,
    workflow_name: str,
    model: str,
    budget: int,
    prefix_cfg: config.PrefixCfg,
    workspace_id: str,
    workspace_state_hash: str,
    code_fingerprint: dict[str, str],
    prod: bool,
) -> list[_PendingCompletion]:
    """Enumerate pending essay × prefix completions.

    For each essay: build a :class:`prepare.PreparedTask`, construct
    the workflow + task, compose the versus config (which yields the
    source_id + config_hash), and skip if a matching row already
    exists.
    """
    from versus.model_config import get_model_config
    from versus.versus_config import make_versus_config

    workflow, task = _make_workflow_and_task(workflow_name, budget=budget)
    mc = get_model_config(model, cfg=cfg)
    db = versus_db.get_client(prod=prod)
    pending: list[_PendingCompletion] = []
    for essay in essays:
        prepared = prepare.prepare(
            essay,
            n_paragraphs=prefix_cfg.n_paragraphs,
            include_headers=prefix_cfg.include_headers,
            length_tolerance=cfg.completion.length_tolerance,
        )
        ctx = EssayPrefixContext(
            essay_id=prepared.essay_id,
            prefix_hash=prepared.prefix_config_hash,
            prefix_text=prepared.prefix_markdown,
            target_length_chars=len(prepared.remainder_markdown),
        )
        base_config, config_hash, _judge_model = make_versus_config(
            workflow=workflow,
            task=task,
            inputs=ctx,
            model=model,
            model_config=mc,
            workspace_id=workspace_id,
            workspace_state_hash=workspace_state_hash,
            code_fingerprint=code_fingerprint,
        )
        source_id = build_source_id(workflow_name, model, config_hash)
        already = _existing_source_ids(
            db, source_id=source_id, prefix_hash=prepared.prefix_config_hash
        )
        if prepared.essay_id in already:
            continue
        pending.append(
            _PendingCompletion(
                task=prepared,
                source_id=source_id,
                config_hash=config_hash,
                base_config=base_config,
            )
        )
    return pending


async def run_orch_completion(
    cfg: config.Config,
    essays: Sequence[Any],
    *,
    workspace: str,
    workflow_name: str,
    model: str,
    budget: int,
    prefix_cfg: config.PrefixCfg,
    limit: int | None = None,
    dry_run: bool = False,
    concurrency: int | None = None,
    persist: bool = False,
    prod: bool = False,
) -> None:
    """Run orch completions for ``essays`` under ``prefix_cfg``.

    Each essay × prefix gets its own rumil Run with a fresh ``run_id``
    so the orchestrator's trace hangs naturally under
    ``/traces/<run_id>``. The workflow's last call is the trace anchor
    on the resulting ``versus_texts`` row's ``params`` blob (the
    closer call when ``produces_artifact=False``; the most recent
    workflow dispatch when ``produces_artifact=True``).
    """
    from rumil.database import DB
    from rumil.settings import get_settings
    from rumil.versus_runner import run_versus
    from versus.model_config import get_model_config
    from versus.versus_config import compute_judge_code_fingerprint, compute_workspace_state_hash

    settings = get_settings()

    if workflow_name not in WORKFLOW_REGISTRY:
        valid = sorted(WORKFLOW_REGISTRY.keys())
        raise SystemExit(f"[err ] unknown workflow {workflow_name!r}; registered: {valid}")

    # Probe is always non-staged: project lookup isn't per-run and
    # needs to see baseline rows.
    probe_db = await DB.create(run_id=str(uuid.uuid4()), prod=prod, staged=False)
    project = await _resolve_workspace(probe_db, workspace)
    probe_db.project_id = project.id
    ws_short = project.id[:8]

    workspace_state_hash = await compute_workspace_state_hash(probe_db)
    code_fingerprint = compute_judge_code_fingerprint()
    mc = get_model_config(model, cfg=cfg)

    pending = _plan_pending(
        cfg,
        essays,
        workflow_name=workflow_name,
        model=model,
        budget=budget,
        prefix_cfg=prefix_cfg,
        workspace_id=ws_short,
        workspace_state_hash=workspace_state_hash,
        code_fingerprint=code_fingerprint,
        prod=prod,
    )
    if limit is not None:
        pending = pending[:limit]
    if not pending:
        print("[info] no pending orch completions")
        return

    effective_concurrency = concurrency if concurrency is not None else 1
    print(
        f"[plan] {len(pending)} orch completions "
        f"(workflow={workflow_name}, model={model}, workspace={workspace}, "
        f"budget={budget}, concurrency={effective_concurrency})"
    )
    if dry_run:
        for pc in pending[:20]:
            print(f"  * {pc.task.essay_id} -> {pc.source_id}")
        if len(pending) > 20:
            print(f"  ... and {len(pending) - 20} more")
        return

    versus_client = versus_db.get_client(prod=prod)
    sem = asyncio.Semaphore(effective_concurrency)
    lock = asyncio.Lock()
    summary = RunSummary()
    done = 0
    total = len(pending)

    async def _exec_one(pc: _PendingCompletion) -> None:
        nonlocal done
        async with sem:
            t0 = time.time()
            run_id = str(uuid.uuid4())
            db = await DB.create(
                run_id=run_id, prod=prod, project_id=project.id, staged=not persist
            )
            try:
                workflow, task = _make_workflow_and_task(workflow_name, budget=budget)
                ctx = EssayPrefixContext(
                    essay_id=pc.task.essay_id,
                    prefix_hash=pc.task.prefix_config_hash,
                    prefix_text=pc.task.prefix_markdown,
                    target_length_chars=len(pc.task.remainder_markdown),
                )
                await db.create_run(
                    name=f"versus-rumil-orch-completion:{workspace}:{pc.task.essay_id}",
                    question_id=None,
                    config={
                        "origin": "versus",
                        "workspace": workspace,
                        "task_name": "complete_essay",
                        "essay_id": pc.task.essay_id,
                        "workflow": workflow_name,
                        "completion_config": pc.base_config,
                        "staged": not persist,
                    },
                )
                print(f"[run] {settings.frontend_url.rstrip('/')}/traces/{run_id}")
                result = await run_versus(
                    db,
                    workflow=workflow,
                    task=task,
                    inputs=ctx,
                    model=model,
                    model_config=mc,
                )
            except Exception as e:
                print(f"[err ] {pc.task.essay_id}: {type(e).__name__}: {e}")
                summary.record_error()
                return

            duration = round(time.time() - t0, 2)
            params = {
                "raw_response_text": result.artifact.raw_response,
                "target_words": pc.task.target_words,
                "duration_s": duration,
                "ts": dt.datetime.now(dt.UTC).isoformat(),
                "provider": "rumil-orch",
                "workflow": workflow_name,
                "budget": budget,
                "model_config": mc.to_record_dict(),
                "rumil_run_id": run_id,
                "rumil_call_id": result.call_id,
                "rumil_question_id": result.question_id,
                "rumil_cost_usd": result.cost_usd,
                "trace_url": result.trace_url,
                "config_hash": pc.config_hash,
                "config": pc.base_config,
                "status": result.status,
            }
            async with lock:
                versus_db.insert_text(
                    versus_client,
                    essay_id=pc.task.essay_id,
                    kind="completion",
                    source_id=pc.source_id,
                    text=result.artifact.text,
                    prefix_hash=pc.task.prefix_config_hash,
                    model_id=model,
                    request=None,
                    response=None,
                    params=params,
                )
                done += 1
                summary.record_success(cost_usd=result.cost_usd or 0.0)
                print(
                    f"[done {done}/{total}] {pc.task.essay_id} {pc.source_id} "
                    f"trace={result.trace_url}"
                )

    try:
        await asyncio.gather(*[_exec_one(pc) for pc in pending])
    finally:
        summary.print("orch completions")
