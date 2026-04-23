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


def _call_one(
    essay_id: str,
    prefix_hash: str,
    a_id: str,
    b_id: str,
    first: judge.Source,
    second: judge.Source,
    criterion: str,
    model: str,
    prompt: str,
    k: str,
    max_tokens: int,
    client: httpx.Client,
) -> dict:
    t0 = time.time()
    resp = anthropic_client.chat(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
        max_tokens=max_tokens,
        client=client,
    )
    text = anthropic_client.extract_text(resp)
    verdict = judge.parse_verdict(text)
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
        "criterion": criterion,
        "judge_model": f"anthropic:{model}",
        "verdict": verdict,
        "winner_source": winner_source,
        "reasoning_text": text,
        "prompt": prompt,
        "ts": dt.datetime.utcnow().isoformat() + "Z",
        "duration_s": round(time.time() - t0, 2),
        "raw_response": resp,
    }


def _plan_tasks(
    cfg: config.Config,
    models: Sequence[str],
) -> list[tuple]:
    groups, prefix_texts = judge.load_sources_by_essay(cfg.storage.completions_log)
    existing = jsonl.keys(cfg.storage.judgments_log)
    tasks: list[tuple] = []
    for (essay_id, prefix_hash), sources in groups.items():
        source_ids = list(sources.keys())
        if not cfg.judging.include_human_as_contestant:
            source_ids = [s for s in source_ids if s != "human"]
        if len(source_ids) < 2:
            continue
        prefix_text = prefix_texts.get((essay_id, prefix_hash), "")
        for a_id, b_id in itertools.combinations(sorted(source_ids), 2):
            src_a = judge.Source(a_id, sources[a_id])
            src_b = judge.Source(b_id, sources[b_id])
            first, second = judge.order_pair(essay_id, src_a, src_b)
            for criterion in cfg.judging.criteria:
                for model in models:
                    judge_model = f"anthropic:{model}"
                    k = judge.judgment_key(
                        essay_id, prefix_hash, a_id, b_id, criterion, judge_model
                    )
                    if k in existing:
                        continue
                    prompt = judge.render_judge_prompt(
                        prefix_text=prefix_text,
                        criterion=criterion,
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
                            prompt,
                            k,
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
) -> None:
    if not models:
        print(
            "[info] no Anthropic judge models configured "
            "(judging.anthropic_models empty and no --model passed); nothing to do"
        )
        return
    tasks = _plan_tasks(cfg, models)
    if limit is not None:
        tasks = tasks[:limit]
    if not tasks:
        print("[info] no pending anthropic judgments")
        return

    print(f"[plan] {len(tasks)} anthropic judgment calls (concurrency={cfg.concurrency})")
    if dry_run:
        for t in tasks[:20]:
            print(f"  * {t[9]}")
        if len(tasks) > 20:
            print(f"  ... and {len(tasks) - 20} more")
        return

    client = httpx.Client(timeout=600.0)
    try:
        with ThreadPoolExecutor(max_workers=cfg.concurrency) as pool:
            futures = {
                pool.submit(_call_one, *t, cfg.judging.max_tokens, client): t[9]  # pyright: ignore[reportCallIssue]
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
from typing import Any


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
) -> list[tuple[_PendingPair, str, bool, str]]:
    """Enumerate pending (pair, task_name, is_versus_criterion, judge_model) tuples.

    ``tasks_spec`` is a list of (task_name, is_versus_criterion). The
    ``compose_judge_model(task_name, is_versus_criterion)`` callback is the
    dedup-safe way to derive the judge_model string for each task.

    Filters (all optional, composable):
    - ``essay_ids``: restrict to pairs from these essays
    - ``contestants``: restrict to pairs where both source_ids are in this list
    - ``vs_human``: restrict to pairs where one side is ``"human"``
    """
    essay_id_set = set(essay_ids) if essay_ids else None
    contestants_set = set(contestants) if contestants else None
    groups, prefix_texts = judge.load_sources_by_essay(cfg.storage.completions_log)
    existing = jsonl.keys(cfg.storage.judgments_log)
    out: list[tuple[_PendingPair, str, bool, str]] = []
    for (essay_id, prefix_hash), sources in groups.items():
        if essay_id_set is not None and essay_id not in essay_id_set:
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
            for task_name, is_versus_crit in tasks_spec:
                judge_model = compose_judge_model(task_name, is_versus_crit)
                criterion_value = task_name if is_versus_crit else f"rumil_{task_name}"
                k = judge.judgment_key(
                    essay_id, prefix_hash, a_id, b_id, criterion_value, judge_model
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
    k = judge.judgment_key(
        pair.essay_id,
        pair.prefix_hash,
        pair.source_a_id,
        pair.source_b_id,
        criterion_value,
        judge_model,
    )
    return {
        "key": k,
        "essay_id": pair.essay_id,
        "prefix_config_hash": pair.prefix_hash,
        "source_a": pair.source_a_id,
        "source_b": pair.source_b_id,
        "display_first": pair.display_first_id,
        "display_second": pair.display_second_id,
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
        "rumil_preference_label": result.preference_label,
        "rumil_cost_usd": result.cost_usd,
    }


def _resolve_task_body(task_name: str, is_versus_criterion: bool) -> str:
    if is_versus_criterion:
        from versus.judge import CRITERION_PROMPTS

        if task_name not in CRITERION_PROMPTS:
            raise ValueError(f"unknown versus criterion: {task_name!r}")
        return CRITERION_PROMPTS[task_name]
    from rumil.versus_bridge import get_rumil_dimension_body

    return get_rumil_dimension_body(task_name)


async def run_ws(
    cfg: config.Config,
    *,
    workspace: str,
    dimensions: Sequence[str],
    versus_criteria: Sequence[str] = (),
    limit: int | None = None,
    dry_run: bool = False,
    concurrency: int = 2,
    essay_ids: Sequence[str] | None = None,
    contestants: Sequence[str] | None = None,
    vs_human: bool = False,
    persist: bool = False,
) -> None:
    """Run the workspace-aware rumil judge against pending pairs.

    ``dimensions`` is a list of essay-adapted rumil dimension names
    (e.g. ``general_quality``, ``grounding``) -- each maps to a prompt
    at ``prompts/versus-<name>.md``.

    ``versus_criteria`` adds task-body-from-versus-criterion entries
    alongside dimensions -- useful for direct comparison with OpenRouter
    judges on the same criterion axis. Judge-model strings carry the
    ``versus_`` prefix so dedup keys differ from dimension-based rows.
    """
    import uuid

    from rumil.database import DB
    from rumil.settings import get_settings
    from rumil.versus_bridge import PairContext, judge_pair_ws_aware

    settings = get_settings()
    model = settings.model
    tasks_spec: list[tuple[str, bool]] = [(d, False) for d in dimensions] + [
        (c, True) for c in versus_criteria
    ]
    if not tasks_spec:
        print("[info] no dimensions or versus criteria specified for ws variant; nothing to do")
        return

    run_id = str(uuid.uuid4())
    db = await DB.create(run_id=run_id, prod=False, staged=not persist)
    project = await db.get_or_create_project(workspace)
    db.project_id = project.id
    ws_short = project.id[:8]

    task_body_cache = {(t, v): _resolve_task_body(t, v) for t, v in tasks_spec}
    from rumil.versus_bridge import compute_prompt_hash

    prompt_hash_cache = {k: compute_prompt_hash(b) for k, b in task_body_cache.items()}

    def _compose(task_name: str, is_versus_crit: bool) -> str:
        suffix = f"versus_{task_name}" if is_versus_crit else task_name
        ph = prompt_hash_cache[(task_name, is_versus_crit)]
        return f"rumil:ws:{model}:{ws_short}:{suffix}:p{ph}"

    tasks = _plan_rumil_pairs(
        cfg,
        tasks_spec,
        _compose,
        essay_ids=essay_ids,
        contestants=contestants,
        vs_human=vs_human,
    )
    if limit is not None:
        tasks = tasks[:limit]
    if not tasks:
        print("[info] no pending rumil ws judgments")
        return

    print(
        f"[plan] {len(tasks)} rumil ws-aware judgments "
        f"(model={model}, workspace={workspace}, concurrency={concurrency})"
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

    await db.create_run(
        name=f"versus-rumil-ws:{workspace}",
        question_id=None,
        config={
            "origin": "versus",
            "variant": "ws",
            "workspace": workspace,
            "dimensions": list(dimensions),
            "versus_criteria": list(versus_criteria),
            "num_pairs": len(tasks),
            "staged": not persist,
        },
    )
    print(f"[run] {settings.frontend_url.rstrip('/')}/traces/{run_id}")

    sem = asyncio.Semaphore(concurrency)
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
                result = await judge_pair_ws_aware(db, pair_ctx, task_body=task_body)
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
    dimensions: Sequence[str],
    versus_criteria: Sequence[str] = (),
    budget: int = 1,
    limit: int | None = None,
    dry_run: bool = False,
    essay_ids: Sequence[str] | None = None,
    contestants: Sequence[str] | None = None,
    vs_human: bool = False,
    persist: bool = False,
) -> None:
    """Run the orchestrator rumil judge against pending pairs.

    Each pair × task gets its own rumil Run with a fresh run_id so the
    orchestrator's trace hangs naturally under /traces/<run_id>. The
    closing call's call_id is what lands in the mirrored row.
    """
    import uuid

    from rumil.database import DB
    from rumil.settings import get_settings
    from rumil.versus_bridge import PairContext, judge_pair_orch

    settings = get_settings()
    model = settings.model
    tasks_spec: list[tuple[str, bool]] = [(d, False) for d in dimensions] + [
        (c, True) for c in versus_criteria
    ]
    if not tasks_spec:
        print("[info] no dimensions or versus criteria specified for orch variant; nothing to do")
        return

    # Probe is always non-staged: it only calls get_or_create_project,
    # which should be visible across runs (project lookup isn't per-run).
    probe_db = await DB.create(run_id=str(uuid.uuid4()), prod=False, staged=False)
    project = await probe_db.get_or_create_project(workspace)
    ws_short = project.id[:8]

    task_body_cache = {(t, v): _resolve_task_body(t, v) for t, v in tasks_spec}
    from rumil.versus_bridge import compute_prompt_hash

    prompt_hash_cache = {k: compute_prompt_hash(b) for k, b in task_body_cache.items()}

    def _compose(task_name: str, is_versus_crit: bool) -> str:
        suffix = f"versus_{task_name}" if is_versus_crit else task_name
        ph = prompt_hash_cache[(task_name, is_versus_crit)]
        return f"rumil:orch:{model}:{ws_short}:b{budget}:{suffix}:p{ph}"

    tasks = _plan_rumil_pairs(
        cfg,
        tasks_spec,
        _compose,
        essay_ids=essay_ids,
        contestants=contestants,
        vs_human=vs_human,
    )
    if limit is not None:
        tasks = tasks[:limit]
    if not tasks:
        print("[info] no pending rumil orch judgments")
        return

    print(
        f"[plan] {len(tasks)} rumil orch judgments "
        f"(model={model}, workspace={workspace}, budget={budget})"
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
    for pair, task_name, is_versus_crit, judge_model in tasks:
        t0 = time.time()
        run_id = str(uuid.uuid4())
        db = await DB.create(run_id=run_id, prod=False, project_id=project.id, staged=not persist)
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
            await db.create_run(
                name=f"versus-rumil-orch:{workspace}:{pair.essay_id}",
                question_id=None,
                config={
                    "origin": "versus",
                    "variant": "orch",
                    "workspace": workspace,
                    "budget": budget,
                    "task_name": effective_task,
                    "essay_id": pair.essay_id,
                    "source_a": pair.source_a_id,
                    "source_b": pair.source_b_id,
                    "staged": not persist,
                },
            )
            print(f"[run] {settings.frontend_url.rstrip('/')}/traces/{run_id}")
            result = await judge_pair_orch(db, pair_ctx, task_body=task_body, budget=budget)
        except Exception as e:
            print(f"[err ] {pair.essay_id} {task_name}: {type(e).__name__}: {e}")
            continue
        criterion_value = f"versus_{task_name}" if is_versus_crit else f"rumil_{task_name}"
        row = _mirror_row(pair, judge_model, criterion_value, result, t0=t0)
        jsonl.append(cfg.storage.judgments_log, row)
        done += 1
        print(
            f"[done {done}/{total}] {row['key']}  "
            f"label={result.preference_label!r}  trace={result.trace_url}"
        )


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
) -> None:
    """Run the single-turn rumil-prompt judge against pending pairs.

    Composes the versus-judge-shell + essay-adapted rumil dimension body
    as the system prompt, passes the essay prefix + both continuations
    inline in the user message, and calls Anthropic directly (no DB,
    no tools, no workspace). This is the "prompt alone" condition -- it
    isolates the prompt-source effect from the tool/workspace effect
    that `ws` bundles together.

    ``judge_model`` is ``rumil:text:<model>:<dim>:p<hash>``, distinct from
    ``anthropic:<model>`` (versus-criterion text) and ``rumil:ws:...``
    (workspace-aware).
    """
    from rumil.versus_bridge import (
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

    def _compose(task_name: str, is_versus_crit: bool) -> str:
        ph = prompt_hash_cache[(task_name, is_versus_crit)]
        return f"rumil:text:{anthropic_model}:{task_name}:p{ph}"

    tasks = _plan_rumil_pairs(
        cfg,
        tasks_spec,
        _compose,
        essay_ids=essay_ids,
        contestants=contestants,
        vs_human=vs_human,
    )
    if limit is not None:
        tasks = tasks[:limit]
    if not tasks:
        print("[info] no pending rumil-text judgments")
        return

    print(
        f"[plan] {len(tasks)} rumil-text judgments "
        f"(model={anthropic_model}, concurrency={cfg.concurrency})"
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
            temperature=0.2,
            max_tokens=cfg.judging.max_tokens,
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
        k = judge.judgment_key(
            pair.essay_id,
            pair.prefix_hash,
            pair.source_a_id,
            pair.source_b_id,
            f"rumil_{task_name}",
            judge_model,
        )
        return {
            "key": k,
            "essay_id": pair.essay_id,
            "prefix_config_hash": pair.prefix_hash,
            "source_a": pair.source_a_id,
            "source_b": pair.source_b_id,
            "display_first": pair.display_first_id,
            "display_second": pair.display_second_id,
            "criterion": f"rumil_{task_name}",
            "judge_model": judge_model,
            "verdict": verdict,
            "winner_source": winner_source,
            "reasoning_text": text,
            "prompt": system,
            "ts": dt.datetime.utcnow().isoformat() + "Z",
            "duration_s": round(time.time() - t0, 2),
            "raw_response": resp,
            "rumil_preference_label": label,
        }

    client = httpx.Client(timeout=600.0)
    try:
        with ThreadPoolExecutor(max_workers=cfg.concurrency) as pool:
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
