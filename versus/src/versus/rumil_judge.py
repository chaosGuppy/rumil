"""Pairwise judging via Anthropic (the rumil-style judge backend).

This is the 'rumil-adjacent' judge path: rumil itself deliberately calls
Anthropic directly for prompts/orchestration, so its judges do too. Reuses
pure helpers from `judge.py` (prompt rendering, pair ordering, dedup key,
verdict parsing) -- only the underlying model call differs.

Judge model strings are recorded as ``anthropic:<model>`` so the versus
/results grid surfaces them alongside OpenRouter judges without any UI
changes.

Workspace-aware variant (multi-turn agent with search_workspace /
load_page tools against a rumil workspace) is deferred; see CLAUDE.md.
"""

from __future__ import annotations

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
