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
import inspect
import time
import types
import typing
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from rumil.orchestrators.draft_and_edit import DraftAndEditWorkflow
from rumil.orchestrators.reflective_judge import ReflectiveJudgeWorkflow
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

# Judge-side workflows. Kept separate from WORKFLOW_REGISTRY so the
# completion CLI's ``--orch <name>`` doesn't accidentally accept a
# judge-only workflow. Only consulted by ``judge_config_is_current`` to
# reproduce a workflow's ``workflow_code_fingerprint`` for staleness
# checks. ``compute_workflow_code_fingerprint`` only reads
# ``workflow.code_paths``, so a placeholder ``dimension_body`` is fine
# for reconstruction — the actual rubric is never re-hashed here.
JUDGE_WORKFLOW_REGISTRY: dict[str, tuple[type, dict[str, Any]]] = {
    "reflective_judge": (
        ReflectiveJudgeWorkflow,
        {"dimension_body": "<staleness-check-placeholder>"},
    ),
}


@dataclass
class _PendingCompletion:
    """One planned (essay × prefix × workflow × model) row to produce."""

    task: prepare.PreparedTask
    source_id: str
    config_hash: str
    base_config: dict[str, Any]


def short_model(model: str) -> str:
    """Compact display form of a model id for run-name labeling.

    Reverses :data:`rumil.settings.RUMIL_MODEL_ALIASES` so e.g.
    ``"claude-sonnet-4-6"`` renders as ``"sonnet"``. Falls back to the
    last ``/``-separated segment for namespaced ids
    (``"anthropic/claude-..."``), and to the bare id otherwise. Used by
    run-name builders (versus orch completions and orch judging) to
    distinguish runs at a glance — see Gap 2 in
    ``planning/orch-experiment-gaps.md``.
    """
    from rumil.settings import RUMIL_MODEL_ALIASES

    reverse = {v: k for k, v in RUMIL_MODEL_ALIASES.items()}
    stripped = model.split("/", 1)[1] if "/" in model else model
    return reverse.get(stripped, model.rsplit("/", 1)[-1])


def build_source_id(workflow_name: str, model: str, config_hash: str) -> str:
    """Compose the canonical ``versus_texts.source_id`` for an orch row.

    Format: ``orch:<workflow_name>:<model>:c<hash8>``. The ``c<hash8>``
    suffix lets two configs of the same workflow (e.g. budget=4 vs
    budget=10) coexist as separate contestants without colliding on
    ``source_id``. Single-shot completions use the bare model id, so
    the ``orch:`` prefix unambiguously distinguishes orch rows.
    """
    return f"orch:{workflow_name}:{model}:c{config_hash[:8]}"


def _accepted_workflow_kwargs(cls: type) -> dict[str, tuple[inspect.Parameter, Any]]:
    """Return the keyword params of ``cls.__init__`` (excluding ``self``).

    Maps ``param_name -> (param, resolved_annotation)`` for the kwargs
    the constructor will accept. Resolves string annotations (which
    ``from __future__ import annotations`` produces) via
    :func:`typing.get_type_hints` so :func:`_coerce_workflow_arg` can
    introspect the actual types instead of literal strings.
    ``budget`` is included here even though it's set by the runner, so
    an unknown-key error message lists it among accepted names.
    """
    sig = inspect.signature(cls.__init__)
    try:
        hints = typing.get_type_hints(cls.__init__)
    except Exception:
        # Constructors with forward refs we can't resolve fall back to
        # the raw annotation string; coercion will treat it as str.
        hints = {}
    return {
        name: (p, hints.get(name, p.annotation))
        for name, p in sig.parameters.items()
        if name != "self"
    }


def _coerce_workflow_arg(name: str, raw: str, annotation: Any) -> Any:
    """Coerce a ``key=value`` string to the type the workflow expects.

    Reads the parameter's resolved annotation and casts:

    - ``int`` (and ``int | None``) → ``int(raw)``
    - ``bool`` (and ``bool | None``) → ``"true"/"false"/"1"/"0"`` etc.
    - ``str`` (and ``str | None``) → ``raw`` as-is
    - anything else → ``raw`` (string passthrough; the constructor's
      own validation surfaces any mismatch)

    ``None``-valued kwargs are reachable via the literal string
    ``"none"`` (case-insensitive) — it maps to Python ``None`` so users
    can clear an inherited default without juggling YAML.
    """
    if raw.lower() == "none":
        return None
    types_in_anno = _flatten_optional(annotation)
    if int in types_in_anno and bool not in types_in_anno:
        try:
            return int(raw)
        except ValueError as e:
            raise ValueError(f"--workflow-arg {name}={raw!r}: expected int") from e
    if bool in types_in_anno:
        lowered = raw.lower()
        if lowered in ("true", "1", "yes"):
            return True
        if lowered in ("false", "0", "no"):
            return False
        raise ValueError(f"--workflow-arg {name}={raw!r}: expected bool")
    return raw


def _flatten_optional(annotation: Any) -> tuple[Any, ...]:
    """Return the set of non-None types in a possibly-Optional annotation.

    Handles ``int``, ``int | None``, ``Optional[int]``, ``Union[int, str]``
    uniformly so :func:`_coerce_workflow_arg` can ask "is int allowed
    here?" without re-implementing the union-walking logic.
    """
    origin = typing.get_origin(annotation)
    if origin is typing.Union or origin is types.UnionType:
        return tuple(a for a in typing.get_args(annotation) if a is not type(None))
    return (annotation,)


def _parse_workflow_args(pairs: Sequence[str], cls: type) -> dict[str, Any]:
    """Parse ``key=value`` strings into a kwargs dict for ``cls``.

    Validates each key against ``cls.__init__``'s signature. Unknown
    keys raise ``ValueError`` listing the accepted names so the caller
    (run_completions.py) can surface a helpful argparse error.
    """
    accepted = _accepted_workflow_kwargs(cls)
    out: dict[str, Any] = {}
    for raw in pairs:
        if "=" not in raw:
            raise ValueError(f"--workflow-arg expects key=value, got {raw!r}")
        key, value = raw.split("=", 1)
        if key not in accepted:
            valid = sorted(k for k in accepted if k != "budget")
            raise ValueError(
                f"--workflow-arg {key!r} not accepted by {cls.__name__}; valid keys: {valid}"
            )
        if key == "budget":
            raise ValueError("--workflow-arg cannot set 'budget'; pass --budget instead.")
        _param, annotation = accepted[key]
        out[key] = _coerce_workflow_arg(key, value, annotation)
    return out


def _make_workflow_and_task(
    workflow_name: str,
    *,
    budget: int,
    extra_kwargs: dict[str, Any] | None = None,
) -> tuple[Any, CompleteEssayTask]:
    """Instantiate the workflow + task pair for a given run.

    Pulls the registered workflow class + defaults from
    :data:`WORKFLOW_REGISTRY` and merges the runtime ``budget`` plus
    any caller-supplied ``extra_kwargs`` (e.g. from ``--workflow-arg``).
    Raises ``KeyError`` (with a list of registered names) if
    ``workflow_name`` isn't registered.
    """
    if workflow_name not in WORKFLOW_REGISTRY:
        valid = sorted(WORKFLOW_REGISTRY.keys())
        raise KeyError(f"unknown workflow {workflow_name!r}; registered: {valid}")
    cls, defaults = WORKFLOW_REGISTRY[workflow_name]
    kwargs: dict[str, Any] = {**defaults, **(extra_kwargs or {}), "budget": budget}
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
    prod: bool,
    workflow_kwargs: dict[str, Any] | None = None,
) -> list[_PendingCompletion]:
    """Enumerate pending essay × prefix completions.

    For each essay: build a :class:`prepare.PreparedTask`, construct
    the workflow + task, compose the versus config (which yields the
    source_id + config_hash), and skip if a matching row already
    exists.
    """
    from versus.model_config import get_model_config
    from versus.versus_config import make_versus_config

    workflow, task = _make_workflow_and_task(
        workflow_name, budget=budget, extra_kwargs=workflow_kwargs
    )
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
        # Don't pass code_fingerprint — let make_versus_config auto-
        # compute the post-#425 split shape (shared + per-workflow).
        # Passing the legacy flat key short-circuits the split and
        # leaves rows missing workflow_code_fingerprint.
        base_config, config_hash, _judge_model = make_versus_config(
            workflow=workflow,
            task=task,
            inputs=ctx,
            model=model,
            model_config=mc,
            workspace_id=workspace_id,
            workspace_state_hash=workspace_state_hash,
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
    workflow_kwargs: dict[str, Any] | None = None,
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
    from versus.versus_config import compute_workspace_state_hash

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
        prod=prod,
        workflow_kwargs=workflow_kwargs,
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
                workflow, task = _make_workflow_and_task(
                    workflow_name, budget=budget, extra_kwargs=workflow_kwargs
                )
                ctx = EssayPrefixContext(
                    essay_id=pc.task.essay_id,
                    prefix_hash=pc.task.prefix_config_hash,
                    prefix_text=pc.task.prefix_markdown,
                    target_length_chars=len(pc.task.remainder_markdown),
                )
                # Run-name template (Gap 2): include the differentiators
                # that actually distinguish runs in the traces list —
                # prefix variant, workflow, model alias, and budget. The
                # old "{workspace}:{essay_id}" shape collided across
                # prefix variants / workflows / models / budgets and
                # made the traces list useless for picking out a
                # specific run.
                run_name = (
                    f"versus-orch-completion:{workspace}:{pc.task.essay_id}"
                    f"@{prefix_cfg.id}:{workflow_name}/{short_model(model)}/b{budget}"
                )
                await db.create_run(
                    name=run_name,
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
