"""Pairwise judging via Anthropic and via rumil's agent/orchestrator paths.

Three variants, all writing into the same data/judgments.jsonl with
judge_model strings that distinguish them:

- ``text`` (``anthropic:<model>``): single-turn Anthropic call using the
  versus judge prompt. No rumil imports. This is v0.

- ``ws`` (``rumil:ws:<model>:<ws>:<task>``): one VERSUS_JUDGE agent call
  via rumil with single-arm workspace-exploration tools against a
  user-chosen rumil workspace. Task is an essay-adapted rumil dimension
  by default (``general_quality``, ``grounding``); ``versus_<crit>`` when
  --use-versus-criteria is passed.

- ``orch`` (``rumil:orch:<model>:<ws>:b<N>:<task>``): full
  TwoPhaseOrchestrator run against a per-pair Question, then a closing
  VERSUS_JUDGE call that emits the 7-point preference label. Budget is
  the orchestrator's research call cap (default: 1, the minimum).

Rumil paths emit trace URLs and mirror them into the judgments row
alongside rumil_call_id / rumil_run_id / rumil_question_id /
rumil_project so the versus UI can surface them.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import itertools
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

from versus import anthropic_client, config, jsonl, judge


def _anthropic_sampling(model: str, max_tokens: int) -> dict:
    """Shared sampling dict for anthropic-direct + rumil-text paths.

    Opus 4.7 deprecates the temperature param on the Messages API (returns
    400), so we omit it. Sonnet/Haiku use temperature=0.0 for determinism.
    Kept in one place so the runtime call and the dedup-hash computation
    see the same value.
    """
    use_temp = None if model.startswith("claude-opus-4-7") else 0.0
    return {"temperature": use_temp, "max_tokens": max_tokens}


def _call_one(
    essay_id: str,
    prefix_hash: str,
    a_id: str,
    b_id: str,
    first: judge.Source,
    second: judge.Source,
    criterion: str,
    model: str,
    judge_model: str,
    system_prompt: str,
    user_prompt: str,
    k: str,
    order: judge.Order,
    max_tokens: int,
    client: httpx.Client,
) -> dict:
    t0 = time.time()
    sampling = _anthropic_sampling(model, max_tokens)
    resp = anthropic_client.chat(
        model=model,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
        temperature=sampling["temperature"],
        max_tokens=sampling["max_tokens"],
        client=client,
    )
    text = anthropic_client.extract_text(resp)
    verdict, preference_label = judge.parse_verdict_from_label(text)
    winner_source = None
    if verdict == "A":
        winner_source = first.source_id
    elif verdict == "B":
        winner_source = second.source_id
    elif verdict == "tie":
        winner_source = "tie"
    return {
        "key": k,
        "essay_id": essay_id,
        "prefix_config_hash": prefix_hash,
        "source_a": a_id,
        "source_b": b_id,
        "display_first": first.source_id,
        "display_second": second.source_id,
        "order": order,
        "criterion": criterion,
        "judge_model": judge_model,
        "verdict": verdict,
        "winner_source": winner_source,
        "preference_label": preference_label,
        "reasoning_text": text,
        "prompt": user_prompt,
        "system_prompt": system_prompt,
        "ts": dt.datetime.utcnow().isoformat() + "Z",
        "duration_s": round(time.time() - t0, 2),
        "raw_response": resp,
        "sampling": sampling,
    }


def _plan_tasks(
    cfg: config.Config,
    models: Sequence[str],
    *,
    essay_ids: Sequence[str] | None = None,
    contestants: Sequence[str] | None = None,
    vs_human: bool = False,
    current_only: bool = False,
    prefix_cfg: config.PrefixCfg | None = None,
) -> list[tuple]:
    from versus import prepare

    essay_id_set = set(essay_ids) if essay_ids else None
    contestants_set = set(contestants) if contestants else None
    current_hashes = (
        prepare.current_prefix_hashes(cfg, cfg.essays.cache_dir, prefix_cfg=prefix_cfg)
        if current_only
        else None
    )
    groups, prefix_texts = judge.load_sources_by_essay(cfg.storage.completions_log)
    existing = jsonl.keys(cfg.storage.judgments_log)
    tasks: list[tuple] = []
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
            src_a = judge.Source(a_id, sources[a_id])
            src_b = judge.Source(b_id, sources[b_id])
            first, second = judge.order_pair(essay_id, src_a, src_b)
            order = judge.order_from_display_first(a_id, b_id, first.source_id)
            for criterion in cfg.judging.criteria:
                for model in models:
                    sampling = _anthropic_sampling(model, cfg.judging.max_tokens)
                    judge_model = judge.compose_judge_model(
                        f"anthropic:{model}", criterion, sampling=sampling
                    )
                    k = judge.judgment_key(
                        essay_id, prefix_hash, a_id, b_id, criterion, judge_model, order
                    )
                    if k in existing:
                        continue
                    system_prompt, user_prompt = judge.render_judge_prompt(
                        prefix_text=prefix_text,
                        dimension=criterion,
                        source_a_text=first.text,
                        source_b_text=second.text,
                    )
                    tasks.append(
                        (
                            essay_id,
                            prefix_hash,
                            a_id,
                            b_id,
                            first,
                            second,
                            criterion,
                            model,
                            judge_model,
                            system_prompt,
                            user_prompt,
                            k,
                            order,
                        )
                    )
                    existing.add(k)
    return tasks


def run(
    cfg: config.Config,
    models: Sequence[str],
    *,
    limit: int | None = None,
    dry_run: bool = False,
    essay_ids: Sequence[str] | None = None,
    contestants: Sequence[str] | None = None,
    vs_human: bool = False,
    current_only: bool = False,
    prefix_cfg: config.PrefixCfg | None = None,
) -> None:
    if not models:
        print(
            "[info] no Anthropic judge models configured "
            "(judging.anthropic_models empty and no --model passed); nothing to do"
        )
        return
    tasks = _plan_tasks(
        cfg,
        models,
        essay_ids=essay_ids,
        contestants=contestants,
        current_only=current_only,
        vs_human=vs_human,
        prefix_cfg=prefix_cfg,
    )
    if limit is not None:
        tasks = tasks[:limit]
    if not tasks:
        print("[info] no pending anthropic judgments")
        return

    print(f"[plan] {len(tasks)} anthropic judgment calls (concurrency={cfg.concurrency})")
    if dry_run:
        for t in tasks[:20]:
            print(f"  * {t[11]}")
        if len(tasks) > 20:
            print(f"  ... and {len(tasks) - 20} more")
        return

    client = httpx.Client(timeout=600.0)
    try:
        with ThreadPoolExecutor(max_workers=cfg.concurrency) as pool:
            futures = {
                pool.submit(_call_one, *t, cfg.judging.max_tokens, client): t[11]  # pyright: ignore[reportCallIssue]
                for t in tasks
            }
            done = 0
            total = len(tasks)
            for fut in as_completed(futures):
                k = futures[fut]
                try:
                    row = fut.result()
                except Exception as e:
                    print(f"[err ] {k}: {e}")
                    continue
                jsonl.append(cfg.storage.judgments_log, row)
                done += 1
                print(f"[done {done}/{total}] {k}")
    finally:
        client.close()


from collections.abc import Callable  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from typing import Any  # noqa: E402


@dataclass
class _PendingPair:
    essay_id: str
    prefix_hash: str
    prefix_text: str
    source_a_id: str
    source_a_text: str
    source_b_id: str
    source_b_text: str
    # Display order (matches versus judge.order_pair): first / second in
    # display order for this (essay_id, pair). Used for downstream display
    # parity with OpenRouter judges.
    display_first_id: str
    display_first_text: str
    display_second_id: str
    display_second_text: str


def _plan_rumil_pairs(
    cfg: config.Config,
    tasks_spec: Sequence[tuple[str, bool]],
    compose_judge_model: Callable[[str, bool], str],
    *,
    essay_ids: Sequence[str] | None = None,
    contestants: Sequence[str] | None = None,
    vs_human: bool = False,
    current_only: bool = False,
    prefix_cfg: config.PrefixCfg | None = None,
) -> list[tuple[_PendingPair, str, bool, str]]:
    """Enumerate pending (pair, task_name, is_versus_criterion, judge_model) tuples.

    ``tasks_spec`` is a list of (task_name, is_versus_criterion). The
    ``compose_judge_model(task_name, is_versus_criterion)`` callback is the
    dedup-safe way to derive the judge_model string for each task.

    Filters (all optional, composable):
    - ``essay_ids``: restrict to pairs from these essays
    - ``contestants``: restrict to pairs where both source_ids are in this list
    - ``vs_human``: restrict to pairs where one side is ``"human"``
    - ``current_only``: skip groups whose prefix_hash isn't current
    - ``prefix_cfg``: which prefix variant counts as current (default canonical)
    """
    from versus import prepare

    essay_id_set = set(essay_ids) if essay_ids else None
    contestants_set = set(contestants) if contestants else None
    current_hashes = (
        prepare.current_prefix_hashes(cfg, cfg.essays.cache_dir, prefix_cfg=prefix_cfg)
        if current_only
        else None
    )
    groups, prefix_texts = judge.load_sources_by_essay(cfg.storage.completions_log)
    existing = jsonl.keys(cfg.storage.judgments_log)
    out: list[tuple[_PendingPair, str, bool, str]] = []
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
            src_a = judge.Source(a_id, sources[a_id])
            src_b = judge.Source(b_id, sources[b_id])
            first, second = judge.order_pair(essay_id, src_a, src_b)
            pair = _PendingPair(
                essay_id=essay_id,
                prefix_hash=prefix_hash,
                prefix_text=prefix_text,
                source_a_id=a_id,
                source_a_text=sources[a_id],
                source_b_id=b_id,
                source_b_text=sources[b_id],
                display_first_id=first.source_id,
                display_first_text=first.text,
                display_second_id=second.source_id,
                display_second_text=second.text,
            )
            order = judge.order_from_display_first(a_id, b_id, first.source_id)
            for task_name, is_versus_crit in tasks_spec:
                judge_model = compose_judge_model(task_name, is_versus_crit)
                criterion_value = task_name if is_versus_crit else f"rumil_{task_name}"
                k = judge.judgment_key(
                    essay_id, prefix_hash, a_id, b_id, criterion_value, judge_model, order
                )
                if k in existing:
                    continue
                out.append((pair, task_name, is_versus_crit, judge_model))
                existing.add(k)
    return out


def _mirror_row(
    pair: _PendingPair,
    judge_model: str,
    criterion_value: str,
    result: Any,
    *,
    t0: float,
) -> dict:
    """Build a judgments.jsonl row for a rumil-judged pair.

    Extra fields (``rumil_call_id``, ``rumil_run_id``, ``rumil_trace_url``,
    ``rumil_question_id``, ``rumil_preference_label``) are non-breaking;
    the versus UI picks them up when present and falls back to the
    OpenRouter-compatible shape when they're absent.
    """
    verdict = result.verdict
    winner_source: str | None = None
    if verdict == "A":
        winner_source = pair.display_first_id
    elif verdict == "B":
        winner_source = pair.display_second_id
    elif verdict == "tie":
        winner_source = "tie"
    order = judge.order_from_display_first(
        pair.source_a_id, pair.source_b_id, pair.display_first_id
    )
    k = judge.judgment_key(
        pair.essay_id,
        pair.prefix_hash,
        pair.source_a_id,
        pair.source_b_id,
        criterion_value,
        judge_model,
        order,
    )
    return {
        "key": k,
        "essay_id": pair.essay_id,
        "prefix_config_hash": pair.prefix_hash,
        "source_a": pair.source_a_id,
        "source_b": pair.source_b_id,
        "display_first": pair.display_first_id,
        "display_second": pair.display_second_id,
        "order": order,
        "criterion": criterion_value,
        "judge_model": judge_model,
        "verdict": verdict,
        "winner_source": winner_source,
        "reasoning_text": result.reasoning_text,
        "prompt": "",
        "ts": dt.datetime.utcnow().isoformat() + "Z",
        "duration_s": round(time.time() - t0, 2),
        "raw_response": None,
        "rumil_call_id": result.call_id,
        "rumil_run_id": result.run_id,
        "rumil_question_id": result.question_id,
        "rumil_trace_url": result.trace_url,
        "preference_label": result.preference_label,
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
            f"versus-criterion task bodies were removed in JUDGE_PROMPT_VERSION=2; "
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
    probe_db = await DB.create(run_id=str(uuid.uuid4()), prod=False, staged=False)
    project = await _resolve_workspace(probe_db, workspace)
    ws_short = project.id[:8]

    task_body_cache = {(t, v): _resolve_task_body(t, v) for t, v in tasks_spec}
    from rumil.versus_bridge import (
        BLIND_JUDGE_VERSION,
        compute_pair_surface_hash,
        compute_prompt_hash,
        compute_tool_prompt_hash,
    )

    prompt_hash_cache = {
        k: compute_prompt_hash(b, with_tools=True) for k, b in task_body_cache.items()
    }
    thash = compute_tool_prompt_hash()
    qhash = compute_pair_surface_hash()

    def _compose(task_name: str, is_versus_crit: bool) -> str:
        suffix = f"versus_{task_name}" if is_versus_crit else task_name
        ph = prompt_hash_cache[(task_name, is_versus_crit)]
        return (
            f"rumil:ws:{model}:{ws_short}:{suffix}:p{ph}:v{BLIND_JUDGE_VERSION}:t{thash}:q{qhash}"
        )

    tasks = _plan_rumil_pairs(
        cfg,
        tasks_spec,
        _compose,
        essay_ids=essay_ids,
        contestants=contestants,
        vs_human=vs_human,
        current_only=current_only,
        prefix_cfg=prefix_cfg,
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
        for pair, task_name, is_versus_crit, judge_model in tasks[:20]:
            kind = "versus" if is_versus_crit else "rumil_dim"
            print(
                f"  * [{kind}:{task_name}] {pair.essay_id} {pair.source_a_id} vs {pair.source_b_id} -> {judge_model}"
            )
        if len(tasks) > 20:
            print(f"  ... and {len(tasks) - 20} more")
        return

    sem = asyncio.Semaphore(effective_concurrency)
    done = 0
    total = len(tasks)
    lock = asyncio.Lock()

    async def _exec_one(
        pair: _PendingPair,
        task_name: str,
        is_versus_crit: bool,
        judge_model: str,
    ) -> None:
        nonlocal done
        async with sem:
            t0 = time.time()
            # Each pair gets its own run_id + its own DB. Matches the
            # run_orch shape: staging is per-run, concurrent pairs can't
            # contaminate each other's staged views, the per-pair trace
            # URL points at just that pair's VERSUS_JUDGE call, and the
            # MutationState cache on the shared DB no longer thrashes
            # across pairs.
            run_id = str(uuid.uuid4())
            db = await DB.create(
                run_id=run_id, prod=False, project_id=project.id, staged=not persist
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
                ph = prompt_hash_cache[(task_name, is_versus_crit)]
                await db.create_run(
                    name=f"versus-rumil-ws:{workspace}:{pair.essay_id}",
                    question_id=None,
                    config={
                        "origin": "versus",
                        "variant": "ws",
                        "workspace": workspace,
                        "model": model,
                        "task_name": effective_task,
                        "prompt_hash": ph,
                        "blind_judge_version": BLIND_JUDGE_VERSION,
                        "essay_id": pair.essay_id,
                        # Canonical alphabetical order for dedup-key
                        # purposes, NOT display order. display_first /
                        # order live on the judgment row in
                        # judgments.jsonl — intentionally not duplicated
                        # here to keep runs.config blind-equivalent with
                        # page.extra if a future change routes config
                        # into agent context.
                        "canonical_source_first": pair.source_a_id,
                        "canonical_source_second": pair.source_b_id,
                        "staged": not persist,
                    },
                )
                print(f"[run] {settings.frontend_url.rstrip('/')}/traces/{run_id}")
                result = await judge_pair_ws_aware(db, pair_ctx, task_body=task_body, model=model)
            except Exception as e:
                print(f"[err ] {pair.essay_id} {task_name}: {type(e).__name__}: {e}")
                return
            criterion_value = f"versus_{task_name}" if is_versus_crit else f"rumil_{task_name}"
            row = _mirror_row(pair, judge_model, criterion_value, result, t0=t0)
            async with lock:
                jsonl.append(cfg.storage.judgments_log, row)
                done += 1
                print(
                    f"[done {done}/{total}] {row['key']}  "
                    f"label={result.preference_label!r}  trace={result.trace_url}"
                )

    await asyncio.gather(*[_exec_one(*t) for t in tasks])


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
    probe_db = await DB.create(run_id=str(uuid.uuid4()), prod=False, staged=False)
    project = await _resolve_workspace(probe_db, workspace)
    ws_short = project.id[:8]

    task_body_cache = {(t, v): _resolve_task_body(t, v) for t, v in tasks_spec}
    from rumil.versus_bridge import (
        BLIND_JUDGE_VERSION,
        compute_pair_surface_hash,
        compute_prompt_hash,
        compute_tool_prompt_hash,
    )

    prompt_hash_cache = {
        k: compute_prompt_hash(b, with_tools=True) for k, b in task_body_cache.items()
    }
    thash = compute_tool_prompt_hash()
    qhash = compute_pair_surface_hash()

    def _compose(task_name: str, is_versus_crit: bool) -> str:
        suffix = f"versus_{task_name}" if is_versus_crit else task_name
        ph = prompt_hash_cache[(task_name, is_versus_crit)]
        return (
            f"rumil:orch:{model}:{ws_short}:b{budget}:{suffix}"
            f":p{ph}:v{BLIND_JUDGE_VERSION}:t{thash}:q{qhash}"
        )

    tasks = _plan_rumil_pairs(
        cfg,
        tasks_spec,
        _compose,
        essay_ids=essay_ids,
        contestants=contestants,
        vs_human=vs_human,
        current_only=current_only,
        prefix_cfg=prefix_cfg,
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
        for pair, task_name, is_versus_crit, judge_model in tasks[:20]:
            kind = "versus" if is_versus_crit else "rumil_dim"
            print(
                f"  * [{kind}:{task_name}] {pair.essay_id} {pair.source_a_id} vs {pair.source_b_id} -> {judge_model}"
            )
        if len(tasks) > 20:
            print(f"  ... and {len(tasks) - 20} more")
        return

    done = 0
    total = len(tasks)
    sem = asyncio.Semaphore(effective_concurrency)
    lock = asyncio.Lock()

    async def _exec_one(
        pair: _PendingPair,
        task_name: str,
        is_versus_crit: bool,
        judge_model: str,
    ) -> None:
        nonlocal done
        async with sem:
            t0 = time.time()
            run_id = str(uuid.uuid4())
            # Each pair gets its own run_id + its own DB. Staging is
            # per-run so concurrent pairs don't contaminate each other's
            # staged views.
            db = await DB.create(
                run_id=run_id, prod=False, project_id=project.id, staged=not persist
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
                # fed to the agent during the run (agent reads pages via
                # load_page / search / explore_subgraph; it never reads
                # the runs row). Safe to embed judge identity AND
                # per-pair metadata for forensic traceability of an
                # individual orch run.
                ph = prompt_hash_cache[(task_name, is_versus_crit)]
                await db.create_run(
                    name=f"versus-rumil-orch:{workspace}:{pair.essay_id}",
                    question_id=None,
                    config={
                        "origin": "versus",
                        "variant": "orch",
                        "workspace": workspace,
                        "model": model,
                        "budget": budget,
                        "task_name": effective_task,
                        "prompt_hash": ph,
                        "blind_judge_version": BLIND_JUDGE_VERSION,
                        "essay_id": pair.essay_id,
                        # Canonical alphabetical order for dedup-key
                        # purposes, NOT display order. display_first /
                        # order live on the judgment row in
                        # judgments.jsonl — intentionally not duplicated
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
                    db, pair_ctx, task_body=task_body, model=model, budget=budget
                )
            except Exception as e:
                print(f"[err ] {pair.essay_id} {task_name}: {type(e).__name__}: {e}")
                return
            criterion_value = f"versus_{task_name}" if is_versus_crit else f"rumil_{task_name}"
            row = _mirror_row(pair, judge_model, criterion_value, result, t0=t0)
            async with lock:
                jsonl.append(cfg.storage.judgments_log, row)
                done += 1
                print(
                    f"[done {done}/{total}] {row['key']}  "
                    f"label={result.preference_label!r}  trace={result.trace_url}"
                )

    await asyncio.gather(*[_exec_one(*t) for t in tasks])


def _build_rumil_text_user_message(pair: _PendingPair, task_name: str) -> str:
    """Compose the user message for a rumil-text judgment.

    Unlike the ws / orch paths (which put the essay prefix + both
    continuations on a Question page that the agent reads via load_page),
    rumil-text has no DB / no tools -- the full pair has to go in the
    user message inline.

    Keeps the continuations in display order and does NOT disclose the
    source_ids (same blind-judge guarantee as ws).
    """
    return (
        f"Compare Continuation A and Continuation B on the dimension "
        f"**{task_name}**.\n\n"
        "End your response with one of the 7-point preference labels "
        "on its own line.\n\n"
        f"## Essay opening\n\n{pair.prefix_text}\n\n"
        f"## Continuation A\n\n{pair.display_first_text}\n\n"
        f"## Continuation B\n\n{pair.display_second_text}\n"
    )


def run_rumil_text(
    cfg: config.Config,
    *,
    anthropic_model: str,
    dimensions: Sequence[str],
    limit: int | None = None,
    dry_run: bool = False,
    essay_ids: Sequence[str] | None = None,
    contestants: Sequence[str] | None = None,
    vs_human: bool = False,
    current_only: bool = False,
    prefix_cfg: config.PrefixCfg | None = None,
    concurrency: int | None = None,
) -> None:
    """Run the single-turn rumil-prompt judge against pending pairs.

    Composes the versus-judge-shell + essay-adapted rumil dimension body
    as the system prompt, passes the essay prefix + both continuations
    inline in the user message, and calls Anthropic directly (no DB,
    no tools, no workspace). This is the "prompt alone" condition -- it
    isolates the prompt-source effect from the tool/workspace effect
    that `ws` bundles together.

    ``judge_model`` is ``rumil:text:<model>:<dim>:p<hash>:v<N>:s<hash>``,
    distinct from ``anthropic:<model>`` (versus-criterion text) and
    ``rumil:ws:...`` (workspace-aware). ``:v<N>`` is ``BLIND_JUDGE_VERSION``
    (parallel to rumil:ws / rumil:orch — prior rumil-text runs had no
    version tag, so they're orphaned by this fork).
    """
    from rumil.versus_bridge import (
        BLIND_JUDGE_VERSION,
        build_system_prompt,
        compute_prompt_hash,
        extract_preference,
        label_to_verdict,
    )

    tasks_spec: list[tuple[str, bool]] = [(d, False) for d in dimensions]
    if not tasks_spec:
        print("[info] no dimensions specified for rumil-text variant; nothing to do")
        return

    task_body_cache = {(t, False): _resolve_task_body(t, False) for t in dimensions}
    prompt_hash_cache = {k: compute_prompt_hash(b) for k, b in task_body_cache.items()}
    system_prompt_cache = {k: build_system_prompt(b) for k, b in task_body_cache.items()}

    effective_concurrency = concurrency if concurrency is not None else cfg.concurrency

    sampling = _anthropic_sampling(anthropic_model, cfg.judging.max_tokens)
    shash = judge.compute_sampling_hash(sampling)

    def _compose(task_name: str, is_versus_crit: bool) -> str:
        ph = prompt_hash_cache[(task_name, is_versus_crit)]
        return f"rumil:text:{anthropic_model}:{task_name}:p{ph}:v{BLIND_JUDGE_VERSION}:s{shash}"

    tasks = _plan_rumil_pairs(
        cfg,
        tasks_spec,
        _compose,
        essay_ids=essay_ids,
        contestants=contestants,
        vs_human=vs_human,
        current_only=current_only,
        prefix_cfg=prefix_cfg,
    )
    if limit is not None:
        tasks = tasks[:limit]
    if not tasks:
        print("[info] no pending rumil-text judgments")
        return

    print(
        f"[plan] {len(tasks)} rumil-text judgments "
        f"(model={anthropic_model}, concurrency={effective_concurrency})"
    )
    if dry_run:
        for pair, task_name, _is_versus_crit, judge_model in tasks[:20]:
            print(
                f"  * [rumil_dim:{task_name}] {pair.essay_id} "
                f"{pair.source_a_id} vs {pair.source_b_id} -> {judge_model}"
            )
        if len(tasks) > 20:
            print(f"  ... and {len(tasks) - 20} more")
        return

    def _call_one_rumil_text(
        pair: _PendingPair,
        task_name: str,
        judge_model: str,
        client: httpx.Client,
    ) -> dict:
        t0 = time.time()
        system = system_prompt_cache[(task_name, False)]
        user_msg = _build_rumil_text_user_message(pair, task_name)
        resp = anthropic_client.chat(
            model=anthropic_model,
            messages=[{"role": "user", "content": user_msg}],
            system=system,
            temperature=sampling["temperature"],
            max_tokens=sampling["max_tokens"],
            client=client,
        )
        text = anthropic_client.extract_text(resp)
        label = extract_preference(text)
        verdict = label_to_verdict(label)
        winner_source: str | None = None
        if verdict == "A":
            winner_source = pair.display_first_id
        elif verdict == "B":
            winner_source = pair.display_second_id
        elif verdict == "tie":
            winner_source = "tie"
        order = judge.order_from_display_first(
            pair.source_a_id, pair.source_b_id, pair.display_first_id
        )
        k = judge.judgment_key(
            pair.essay_id,
            pair.prefix_hash,
            pair.source_a_id,
            pair.source_b_id,
            f"rumil_{task_name}",
            judge_model,
            order,
        )
        return {
            "key": k,
            "essay_id": pair.essay_id,
            "prefix_config_hash": pair.prefix_hash,
            "source_a": pair.source_a_id,
            "source_b": pair.source_b_id,
            "display_first": pair.display_first_id,
            "display_second": pair.display_second_id,
            "order": order,
            "criterion": f"rumil_{task_name}",
            "judge_model": judge_model,
            "verdict": verdict,
            "winner_source": winner_source,
            "reasoning_text": text,
            "prompt": user_msg,
            "system_prompt": system,
            "ts": dt.datetime.utcnow().isoformat() + "Z",
            "duration_s": round(time.time() - t0, 2),
            "raw_response": resp,
            "rumil_preference_label": label,
            "sampling": sampling,
        }

    client = httpx.Client(timeout=600.0)
    try:
        with ThreadPoolExecutor(max_workers=effective_concurrency) as pool:
            futures = {
                pool.submit(
                    _call_one_rumil_text, pair, task_name, judge_model, client
                ): judge.judgment_key(
                    pair.essay_id,
                    pair.prefix_hash,
                    pair.source_a_id,
                    pair.source_b_id,
                    f"rumil_{task_name}",
                    judge_model,
                    judge.order_from_display_first(
                        pair.source_a_id, pair.source_b_id, pair.display_first_id
                    ),
                )
                for pair, task_name, _ivc, judge_model in tasks
            }
            done = 0
            total = len(tasks)
            for fut in as_completed(futures):
                k = futures[fut]
                try:
                    row = fut.result()
                except Exception as e:
                    print(f"[err ] {k}: {e}")
                    continue
                jsonl.append(cfg.storage.judgments_log, row)
                done += 1
                print(f"[done {done}/{total}] {k}  label={row.get('rumil_preference_label')!r}")
    finally:
        client.close()
