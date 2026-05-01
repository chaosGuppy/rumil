"""Workspace-aware pairwise judging via rumil's agent/orchestrator paths.

Two variants, both writing rows into ``versus_judgments`` with
``judge_model`` strings that distinguish them:

- ``ws`` (``rumil:ws:<model>:<ws>:<task>``): one VERSUS_JUDGE agent call
  via rumil with single-arm workspace-exploration tools against a
  user-chosen rumil workspace. Task is an essay-adapted rumil dimension
  (``general_quality``, ``grounding``).

- ``orch`` (``rumil:orch:<model>:<ws>:b<N>:<task>``): full
  TwoPhaseOrchestrator run against a per-pair Question, then a closing
  VERSUS_JUDGE call that emits the 7-point preference label. Budget is
  the orchestrator's research call cap (minimum: 4).

Both populate ``project_id`` / ``run_id`` / ``rumil_call_id`` on the
judgment row so the versus UI can surface trace URLs back to rumil.

The blind (no-tools) judge paths — formerly ``text`` and ``rumil-text``
— moved into :func:`versus.judge.run_blind`.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from versus import config, judge, versus_db
from versus.run_summary import RunSummary


@dataclass
class _PendingPair:
    essay_id: str
    prefix_hash: str
    prefix_text: str
    source_a_id: str
    source_a_text: str
    source_a_text_id: str
    source_b_id: str
    source_b_text: str
    source_b_text_id: str
    # Display order (matches versus judge.order_pair): first / second in
    # display order for this (essay_id, pair). Used for downstream display
    # parity with OpenRouter judges.
    display_first_id: str
    display_first_text: str
    display_second_id: str
    display_second_text: str


# Returned per pending judgment by _plan_rumil_pairs. The variant arg
# (ws/orch) lives on the make_judge_config call upstream — we just thread
# the resulting base_config through so callers can fold in text_a_id /
# text_b_id at insert time.
@dataclass
class _PendingJudgment:
    pair: _PendingPair
    task_name: str
    is_versus_crit: bool
    judge_model: str
    base_config: dict


def _plan_rumil_pairs(
    cfg: config.Config,
    tasks_spec: Sequence[tuple[str, bool]],
    compose_judge_config: Callable[[str, bool], tuple[dict, str, str]],
    *,
    essay_ids: Sequence[str] | None = None,
    contestants: Sequence[str] | None = None,
    vs_human: bool = False,
    current_only: bool = False,
    prefix_cfg: config.PrefixCfg | None = None,
    prod: bool = False,
) -> list[_PendingJudgment]:
    """Enumerate pending judgments (skipping ones already in versus_judgments).

    The ``compose_judge_config(task_name, is_versus_criterion)`` callback
    delegates to :func:`versus.judge_config.make_judge_config` so the
    structured config and the legacy-shape ``judge_model`` come from one
    source of truth. Dedup is done content-addressed: we compute the
    judge_inputs_hash that would land on the new row (with text_a_id/
    text_b_id baked in) and skip if a row already exists at that key.

    Filters (all optional, composable):
    - ``essay_ids``: restrict to pairs from these essays
    - ``contestants``: restrict to pairs where both source_ids are in this list
    - ``vs_human``: restrict to pairs where one side is ``"human"``
    - ``current_only``: skip groups whose prefix_hash isn't the current
      canonical hash for the essay (or the requested ``prefix_cfg`` variant)
    - ``prefix_cfg``: when non-None, hard-restrict to rows whose prefix_hash
      matches that variant's current hash. Implies current-only filtering
      for that variant.
    """
    from versus import prepare

    essay_id_set = set(essay_ids) if essay_ids else None
    contestants_set = set(contestants) if contestants else None
    db = versus_db.get_client(prod=prod)
    current_hashes = (
        prepare.current_prefix_hashes(cfg, prefix_cfg=prefix_cfg, client=db)
        if (current_only or prefix_cfg is not None)
        else None
    )
    groups, prefix_texts = judge.load_sources_by_essay(db)
    existing = judge._existing_judgment_keys(db)
    out: list[_PendingJudgment] = []
    for (essay_id, prefix_hash), sources in groups.items():
        if essay_id_set is not None and essay_id not in essay_id_set:
            continue
        if current_hashes is not None and current_hashes.get(essay_id) != prefix_hash:
            continue
        source_ids = list(sources.keys())
        if not cfg.judging.include_human_as_contestant:
            source_ids = [s for s in source_ids if s != "human"]
        if contestants_set is not None:
            source_ids = [s for s in source_ids if s in contestants_set]
        if len(source_ids) < 2:
            continue
        prefix_text = prefix_texts.get((essay_id, prefix_hash), "")
        for a_id, b_id in itertools.combinations(sorted(source_ids), 2):
            if vs_human and "human" not in (a_id, b_id):
                continue
            src_a = sources[a_id]
            src_b = sources[b_id]
            first, second = judge.order_pair(essay_id, src_a, src_b)
            pair = _PendingPair(
                essay_id=essay_id,
                prefix_hash=prefix_hash,
                prefix_text=prefix_text,
                source_a_id=a_id,
                source_a_text=src_a.text,
                source_a_text_id=src_a.text_id,
                source_b_id=b_id,
                source_b_text=src_b.text,
                source_b_text_id=src_b.text_id,
                display_first_id=first.source_id,
                display_first_text=first.text,
                display_second_id=second.source_id,
                display_second_text=second.text,
            )
            order = judge.order_from_display_first(a_id, b_id, first.source_id)
            for task_name, is_versus_crit in tasks_spec:
                base_config, _, judge_model = compose_judge_config(task_name, is_versus_crit)
                criterion_value = task_name if is_versus_crit else f"rumil_{task_name}"
                # Predict the judge_inputs_hash that would land on the row, so
                # we skip pairs that already have a row at this exact config.
                predicted_inputs, predicted_hash = judge._build_judge_inputs(
                    base_config, src_a.text_id, src_b.text_id, order
                )
                del predicted_inputs  # only the hash is used for dedup
                key = (essay_id, prefix_hash, a_id, b_id, criterion_value, predicted_hash)
                if key in existing:
                    continue
                out.append(
                    _PendingJudgment(
                        pair=pair,
                        task_name=task_name,
                        is_versus_crit=is_versus_crit,
                        judge_model=judge_model,
                        base_config=base_config,
                    )
                )
                existing.add(key)
    return out


def _mirror_row(
    pair: _PendingPair,
    judge_model: str,
    criterion_value: str,
    result: Any,
    *,
    t0: float,
    judge_inputs: dict,
    variant: str,
) -> dict:
    """Build a versus_judgments row for a rumil-judged pair.

    Returns a dict shaped for :func:`versus_db.insert_judgment`. The
    ``judge_inputs`` blob already carries the rumil judge config (model,
    sampling, prompt hash, tools, workspace state, etc.); we add the
    text id refs and the order axis so the row's content-addressed hash
    distinguishes both replicates and orientations.
    """
    verdict = result.verdict
    order = judge.order_from_display_first(
        pair.source_a_id, pair.source_b_id, pair.display_first_id
    )
    judge_inputs = dict(judge_inputs)
    judge_inputs["text_a_id"] = pair.source_a_text_id
    judge_inputs["text_b_id"] = pair.source_b_text_id
    judge_inputs["order"] = order
    return {
        "essay_id": pair.essay_id,
        "prefix_hash": pair.prefix_hash,
        "source_a": pair.source_a_id,
        "source_b": pair.source_b_id,
        "display_first": pair.display_first_id,
        "text_a_id": pair.source_a_text_id,
        "text_b_id": pair.source_b_text_id,
        "criterion": criterion_value,
        "variant": variant,
        "judge_model": judge_model,
        "judge_inputs": judge_inputs,
        "verdict": verdict,
        "preference_label": result.preference_label,
        "reasoning_text": result.reasoning_text,
        "duration_s": round(time.time() - t0, 2),
        "rumil_call_id": result.call_id,
        "rumil_run_id": result.run_id,
        "rumil_question_id": result.question_id,
        "rumil_cost_usd": result.cost_usd,
    }


async def _resolve_workspace(db: Any, name: str) -> Any:
    """Look up a workspace by name, failing loudly if it doesn't exist.

    Previously this used ``get_or_create_project``, which silently created
    an empty project on typos and then planned zero judgments. That made
    the failure mode look identical to "already all judged" — a
    misleading ``[info] no pending ...`` with no indication the name was
    wrong. Now we fail early with the list of known workspaces so the
    user can fix the flag.
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


def _resolve_task_body(task_name: str, is_versus_criterion: bool) -> str:
    """Return the dimension body for a judge task.

    The ``is_versus_criterion`` parameter is kept for callsite stability
    after the criterion-prompts removal. Versus-criterion task bodies no
    longer exist -- if any caller still passes True, raise so the issue
    surfaces immediately rather than silently producing rumil-dimension
    output under a versus-criterion judge_model.
    """
    if is_versus_criterion:
        raise ValueError(
            f"versus-criterion task bodies have been removed; "
            f"got task_name={task_name!r}. Pass the rumil dimension name instead "
            f"(e.g. 'general_quality')."
        )
    from rumil.versus_bridge import get_rumil_dimension_body

    return get_rumil_dimension_body(task_name)


async def run_ws(
    cfg: config.Config,
    *,
    workspace: str,
    model: str,
    dimensions: Sequence[str],
    limit: int | None = None,
    dry_run: bool = False,
    concurrency: int | None = None,
    essay_ids: Sequence[str] | None = None,
    contestants: Sequence[str] | None = None,
    vs_human: bool = False,
    current_only: bool = False,
    prefix_cfg: config.PrefixCfg | None = None,
    persist: bool = False,
    prod: bool = False,
) -> None:
    """Run the workspace-aware rumil judge against pending pairs.

    ``model`` is the Anthropic model id the bridge runs the agent on;
    passed explicitly so versus controls it without env-var ordering
    gymnastics. It's the caller's job to resolve aliases (opus/sonnet/
    haiku) to full ids.

    ``dimensions`` is a list of essay-adapted rumil dimension names
    (e.g. ``general_quality``, ``grounding``) -- each maps to a prompt
    at ``prompts/versus-<name>.md``.
    """
    import uuid

    from rumil.database import DB
    from rumil.settings import get_settings
    from rumil.versus_bridge import PairContext, judge_pair_ws_aware

    settings = get_settings()
    tasks_spec: list[tuple[str, bool]] = [(d, False) for d in dimensions]
    if not tasks_spec:
        print("[info] no dimensions specified for ws variant; nothing to do")
        return

    # Probe DB just for project lookup — projects live outside any run's
    # staged view, and the per-pair DBs below inherit db.project_id through
    # their explicit constructor arg.
    probe_db = await DB.create(run_id=str(uuid.uuid4()), prod=prod, staged=False)
    project = await _resolve_workspace(probe_db, workspace)
    probe_db.project_id = project.id
    ws_short = project.id[:8]

    task_body_cache = {(t, v): _resolve_task_body(t, v) for t, v in tasks_spec}
    from rumil.versus_bridge import (
        compute_pair_surface_hash,
        compute_prompt_hash,
        compute_tool_prompt_hash,
    )
    from versus.judge_config import (
        compute_judge_code_fingerprint,
        compute_workspace_state_hash,
        make_judge_config,
    )

    prompt_hash_cache = {
        k: compute_prompt_hash(b, with_tools=True) for k, b in task_body_cache.items()
    }
    thash = compute_tool_prompt_hash()
    qhash = compute_pair_surface_hash()
    code_fingerprint = compute_judge_code_fingerprint()
    # Cheap watermark over baseline pages + links + mutation events so
    # ws judgments fork config_hash when the underlying workspace
    # mutates between runs. Read on the non-staged probe DB so two
    # concurrent pairs see the same baseline.
    workspace_state_hash = await compute_workspace_state_hash(probe_db)

    # Versus model registry is the source of truth for what versus sends on
    # the wire. Look up once; the same ModelConfig is passed to the bridge
    # (which threads it into the SDK agent + closer) AND recorded in
    # judge_inputs so the dedup hash forks on registry edits.
    from versus.model_config import get_judge_model_config

    mc = get_judge_model_config(model, cfg=cfg)
    sampling = {
        "temperature": mc.temperature,
        "max_tokens": mc.max_tokens,
    }
    thinking = mc.thinking
    effort = mc.effort

    def _compose_config(task_name: str, is_versus_crit: bool) -> tuple[dict, str, str]:
        dim = f"versus_{task_name}" if is_versus_crit else task_name
        ph = prompt_hash_cache[(task_name, is_versus_crit)]
        return make_judge_config(
            "ws",
            model=model,
            dimension=dim,
            model_config=mc,
            prompt_hash=ph,
            tool_prompt_hash=thash,
            pair_surface_hash=qhash,
            workspace_id=ws_short,
            code_fingerprint=code_fingerprint,
            workspace_state_hash=workspace_state_hash,
        )

    tasks = _plan_rumil_pairs(
        cfg,
        tasks_spec,
        _compose_config,
        essay_ids=essay_ids,
        contestants=contestants,
        vs_human=vs_human,
        current_only=current_only,
        prefix_cfg=prefix_cfg,
        prod=prod,
    )
    if limit is not None:
        tasks = tasks[:limit]
    if not tasks:
        print("[info] no pending rumil ws judgments")
        return

    effective_concurrency = concurrency if concurrency is not None else 2
    print(
        f"[plan] {len(tasks)} rumil ws-aware judgments "
        f"(model={model}, workspace={workspace}, concurrency={effective_concurrency})"
    )
    if dry_run:
        for pj in tasks[:20]:
            kind = "versus" if pj.is_versus_crit else "rumil_dim"
            pair = pj.pair
            print(
                f"  * [{kind}:{pj.task_name}] {pair.essay_id} {pair.source_a_id} vs "
                f"{pair.source_b_id} -> {pj.judge_model}"
            )
        if len(tasks) > 20:
            print(f"  ... and {len(tasks) - 20} more")
        return

    sem = asyncio.Semaphore(effective_concurrency)
    done = 0
    total = len(tasks)
    lock = asyncio.Lock()
    summary = RunSummary()

    versus_client = versus_db.get_client(prod=prod)

    async def _exec_one(pj: _PendingJudgment) -> None:
        nonlocal done
        pair = pj.pair
        task_name = pj.task_name
        is_versus_crit = pj.is_versus_crit
        async with sem:
            t0 = time.time()
            # Each pair gets its own run_id + its own DB. Staging is
            # per-run, concurrent pairs can't contaminate each other's
            # staged views, the per-pair trace URL points at just that
            # pair's VERSUS_JUDGE call, and the MutationState cache on
            # the shared DB no longer thrashes across pairs.
            run_id = str(uuid.uuid4())
            db = await DB.create(
                run_id=run_id, prod=prod, project_id=project.id, staged=not persist
            )
            try:
                task_body = _resolve_task_body(task_name, is_versus_crit)
                effective_task = f"versus_{task_name}" if is_versus_crit else task_name
                pair_ctx = PairContext(
                    essay_id=pair.essay_id,
                    prefix_hash=pair.prefix_hash,
                    prefix_text=pair.prefix_text,
                    continuation_a_id=pair.display_first_id,
                    continuation_a_text=pair.display_first_text,
                    continuation_b_id=pair.display_second_id,
                    continuation_b_text=pair.display_second_text,
                    source_a_id=pair.source_a_id,
                    source_b_id=pair.source_b_id,
                    task_name=effective_task,
                )
                # runs.config is surfaced via the traces UI but is NOT
                # fed to the agent (agent reads pages via load_page /
                # search / explore_subgraph, never the runs row). Safe
                # to embed per-pair metadata for forensic traceability.
                await db.create_run(
                    name=f"versus-rumil-ws:{workspace}:{pair.essay_id}",
                    question_id=None,
                    config={
                        "origin": "versus",
                        "workspace": workspace,
                        "task_name": effective_task,
                        "essay_id": pair.essay_id,
                        "judge_config": pj.base_config,
                        "judging_max_tokens": cfg.judging.max_tokens,
                        # Canonical alphabetical order for dedup-key
                        # purposes, NOT display order. display_first /
                        # order live on the judgment row in
                        # versus_judgments — intentionally not duplicated
                        # here to keep runs.config blind-equivalent with
                        # page.extra if a future change routes config
                        # into agent context.
                        "canonical_source_first": pair.source_a_id,
                        "canonical_source_second": pair.source_b_id,
                        "staged": not persist,
                    },
                )
                print(f"[run] {settings.frontend_url.rstrip('/')}/traces/{run_id}")
                result = await judge_pair_ws_aware(
                    db, pair_ctx, task_body=task_body, model=model, model_config=mc
                )
            except Exception as e:
                print(f"[err ] {pair.essay_id} {task_name}: {type(e).__name__}: {e}")
                summary.record_error()
                return
            criterion_value = f"versus_{task_name}" if is_versus_crit else f"rumil_{task_name}"
            row = _mirror_row(
                pair,
                pj.judge_model,
                criterion_value,
                result,
                t0=t0,
                judge_inputs=pj.base_config,
                variant="ws",
            )
            async with lock:
                versus_db.insert_judgment(
                    versus_client,
                    essay_id=row["essay_id"],
                    prefix_hash=row["prefix_hash"],
                    source_a=row["source_a"],
                    source_b=row["source_b"],
                    display_first=row["display_first"],
                    text_a_id=row["text_a_id"],
                    text_b_id=row["text_b_id"],
                    criterion=row["criterion"],
                    variant=row["variant"],
                    judge_model=row["judge_model"],
                    judge_inputs=row["judge_inputs"],
                    verdict=row["verdict"],
                    reasoning_text=row["reasoning_text"],
                    preference_label=row["preference_label"],
                    duration_s=row["duration_s"],
                    project_id=project.id,
                    run_id=run_id,
                    rumil_call_id=row["rumil_call_id"],
                    rumil_question_id=row["rumil_question_id"],
                    rumil_cost_usd=row["rumil_cost_usd"],
                )
                done += 1
                summary.record_success(cost_usd=result.cost_usd or 0.0)
                print(
                    f"[done {done}/{total}] {pair.essay_id} {pair.source_a_id} vs "
                    f"{pair.source_b_id} [{criterion_value}] "
                    f"label={result.preference_label!r} trace={result.trace_url}"
                )

    try:
        await asyncio.gather(*[_exec_one(pj) for pj in tasks])
    finally:
        summary.print("ws judgments")


async def run_orch(
    cfg: config.Config,
    *,
    workspace: str,
    model: str,
    dimensions: Sequence[str],
    budget: int = 1,
    limit: int | None = None,
    dry_run: bool = False,
    concurrency: int | None = None,
    essay_ids: Sequence[str] | None = None,
    contestants: Sequence[str] | None = None,
    vs_human: bool = False,
    persist: bool = False,
    current_only: bool = False,
    prefix_cfg: config.PrefixCfg | None = None,
    prod: bool = False,
) -> None:
    """Run the orchestrator rumil judge against pending pairs.

    ``model`` is the Anthropic model id the bridge (and the
    orchestrator's nested LLM calls) runs on; passed explicitly so
    versus controls it without env-var ordering gymnastics.

    Each pair × task gets its own rumil Run with a fresh run_id so the
    orchestrator's trace hangs naturally under /traces/<run_id>. The
    closing call's call_id is what lands in the mirrored row.
    """
    import uuid

    from rumil.database import DB
    from rumil.settings import get_settings
    from rumil.versus_bridge import PairContext, judge_pair_orch

    settings = get_settings()
    tasks_spec: list[tuple[str, bool]] = [(d, False) for d in dimensions]
    if not tasks_spec:
        print("[info] no dimensions specified for orch variant; nothing to do")
        return

    # Probe is always non-staged: project lookup isn't per-run and
    # needs to see baseline rows.
    probe_db = await DB.create(run_id=str(uuid.uuid4()), prod=prod, staged=False)
    project = await _resolve_workspace(probe_db, workspace)
    probe_db.project_id = project.id
    ws_short = project.id[:8]

    task_body_cache = {(t, v): _resolve_task_body(t, v) for t, v in tasks_spec}
    from rumil.versus_bridge import (
        compute_orch_closer_hash,
        compute_pair_surface_hash,
        compute_prompt_hash,
        compute_tool_prompt_hash,
    )
    from versus.judge_config import (
        compute_judge_code_fingerprint,
        compute_workspace_state_hash,
        make_judge_config,
    )

    prompt_hash_cache = {
        k: compute_prompt_hash(b, with_tools=True) for k, b in task_body_cache.items()
    }
    thash = compute_tool_prompt_hash()
    qhash = compute_pair_surface_hash()
    chash = compute_orch_closer_hash()
    code_fingerprint = compute_judge_code_fingerprint()
    workspace_state_hash = await compute_workspace_state_hash(probe_db)

    # Versus model registry as source of truth (see run_ws above).
    from versus.model_config import get_judge_model_config

    mc = get_judge_model_config(model, cfg=cfg)
    sampling = {
        "temperature": mc.temperature,
        "max_tokens": mc.max_tokens,
    }
    thinking = mc.thinking
    effort = mc.effort

    def _compose_config(task_name: str, is_versus_crit: bool) -> tuple[dict, str, str]:
        dim = f"versus_{task_name}" if is_versus_crit else task_name
        ph = prompt_hash_cache[(task_name, is_versus_crit)]
        return make_judge_config(
            "orch",
            model=model,
            dimension=dim,
            model_config=mc,
            prompt_hash=ph,
            tool_prompt_hash=thash,
            pair_surface_hash=qhash,
            workspace_id=ws_short,
            budget=budget,
            closer_hash=chash,
            code_fingerprint=code_fingerprint,
            workspace_state_hash=workspace_state_hash,
        )

    tasks = _plan_rumil_pairs(
        cfg,
        tasks_spec,
        _compose_config,
        essay_ids=essay_ids,
        contestants=contestants,
        vs_human=vs_human,
        current_only=current_only,
        prefix_cfg=prefix_cfg,
        prod=prod,
    )
    if limit is not None:
        tasks = tasks[:limit]
    if not tasks:
        print("[info] no pending rumil orch judgments")
        return

    effective_concurrency = concurrency if concurrency is not None else 1
    print(
        f"[plan] {len(tasks)} rumil orch judgments "
        f"(model={model}, workspace={workspace}, budget={budget}, "
        f"concurrency={effective_concurrency})"
    )
    if dry_run:
        for pj in tasks[:20]:
            kind = "versus" if pj.is_versus_crit else "rumil_dim"
            pair = pj.pair
            print(
                f"  * [{kind}:{pj.task_name}] {pair.essay_id} {pair.source_a_id} vs "
                f"{pair.source_b_id} -> {pj.judge_model}"
            )
        if len(tasks) > 20:
            print(f"  ... and {len(tasks) - 20} more")
        return

    done = 0
    total = len(tasks)
    sem = asyncio.Semaphore(effective_concurrency)
    lock = asyncio.Lock()
    summary = RunSummary()

    versus_client = versus_db.get_client(prod=prod)

    async def _exec_one(pj: _PendingJudgment) -> None:
        nonlocal done
        pair = pj.pair
        task_name = pj.task_name
        is_versus_crit = pj.is_versus_crit
        async with sem:
            t0 = time.time()
            run_id = str(uuid.uuid4())
            # Each pair gets its own run_id + its own DB. Staging is
            # per-run so concurrent pairs don't contaminate each other's
            # staged views.
            db = await DB.create(
                run_id=run_id, prod=prod, project_id=project.id, staged=not persist
            )
            try:
                task_body = _resolve_task_body(task_name, is_versus_crit)
                effective_task = f"versus_{task_name}" if is_versus_crit else task_name
                pair_ctx = PairContext(
                    essay_id=pair.essay_id,
                    prefix_hash=pair.prefix_hash,
                    prefix_text=pair.prefix_text,
                    continuation_a_id=pair.display_first_id,
                    continuation_a_text=pair.display_first_text,
                    continuation_b_id=pair.display_second_id,
                    continuation_b_text=pair.display_second_text,
                    source_a_id=pair.source_a_id,
                    source_b_id=pair.source_b_id,
                    task_name=effective_task,
                )
                # runs.config is surfaced via the traces UI but is NOT
                # fed to the agent during the run. Safe to embed
                # per-pair metadata for forensic traceability.
                await db.create_run(
                    name=f"versus-rumil-orch:{workspace}:{pair.essay_id}",
                    question_id=None,
                    config={
                        "origin": "versus",
                        "workspace": workspace,
                        "task_name": effective_task,
                        "essay_id": pair.essay_id,
                        "judge_config": pj.base_config,
                        "judging_max_tokens": cfg.judging.max_tokens,
                        # Canonical alphabetical order for dedup-key
                        # purposes, NOT display order. display_first /
                        # order live on the judgment row in
                        # versus_judgments — intentionally not duplicated
                        # here to keep runs.config blind-equivalent with
                        # page.extra if a future change routes config
                        # into agent context.
                        "canonical_source_first": pair.source_a_id,
                        "canonical_source_second": pair.source_b_id,
                        "staged": not persist,
                    },
                )
                print(f"[run] {settings.frontend_url.rstrip('/')}/traces/{run_id}")
                result = await judge_pair_orch(
                    db,
                    pair_ctx,
                    task_body=task_body,
                    model=model,
                    budget=budget,
                    model_config=mc,
                )
            except Exception as e:
                print(f"[err ] {pair.essay_id} {task_name}: {type(e).__name__}: {e}")
                summary.record_error()
                return
            criterion_value = f"versus_{task_name}" if is_versus_crit else f"rumil_{task_name}"
            row = _mirror_row(
                pair,
                pj.judge_model,
                criterion_value,
                result,
                t0=t0,
                judge_inputs=pj.base_config,
                variant="orch",
            )
            async with lock:
                versus_db.insert_judgment(
                    versus_client,
                    essay_id=row["essay_id"],
                    prefix_hash=row["prefix_hash"],
                    source_a=row["source_a"],
                    source_b=row["source_b"],
                    display_first=row["display_first"],
                    text_a_id=row["text_a_id"],
                    text_b_id=row["text_b_id"],
                    criterion=row["criterion"],
                    variant=row["variant"],
                    judge_model=row["judge_model"],
                    judge_inputs=row["judge_inputs"],
                    verdict=row["verdict"],
                    reasoning_text=row["reasoning_text"],
                    preference_label=row["preference_label"],
                    duration_s=row["duration_s"],
                    project_id=project.id,
                    run_id=run_id,
                    rumil_call_id=row["rumil_call_id"],
                    rumil_question_id=row["rumil_question_id"],
                    rumil_cost_usd=row["rumil_cost_usd"],
                )
                done += 1
                summary.record_success(cost_usd=result.cost_usd or 0.0)
                print(
                    f"[done {done}/{total}] {pair.essay_id} {pair.source_a_id} vs "
                    f"{pair.source_b_id} [{criterion_value}] "
                    f"label={result.preference_label!r} trace={result.trace_url}"
                )

    try:
        await asyncio.gather(*[_exec_one(pj) for pj in tasks])
    finally:
        summary.print("orch judgments")
