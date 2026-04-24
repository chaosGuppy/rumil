"""Run model completions for prepared tasks, with dedup caching."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

from versus import config, jsonl, openrouter, prepare
from versus import essay as versus_essay

HUMAN_SOURCE_ID = "human"


def sampling_hash(model_cfg: config.ModelCfg) -> str:
    blob = json.dumps(model_cfg.model_dump(exclude={"id"}), sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:10]


def completion_key(essay_id: str, prefix_hash: str, source_id: str, samp_hash: str) -> str:
    return f"{essay_id}|{prefix_hash}|{source_id}|{samp_hash}"


def human_key(essay_id: str, prefix_hash: str) -> str:
    return completion_key(essay_id, prefix_hash, HUMAN_SOURCE_ID, "-")


def ensure_human_baseline(
    task: prepare.PreparedTask,
    log_path: pathlib.Path,
    existing_keys: set[str],
) -> None:
    k = human_key(task.essay_id, task.prefix_config_hash)
    if k in existing_keys:
        return
    row = {
        "key": k,
        "essay_id": task.essay_id,
        "prefix_config_hash": task.prefix_config_hash,
        "source_id": HUMAN_SOURCE_ID,
        "source_kind": "human",
        "model_id": HUMAN_SOURCE_ID,
        "sampling_hash": "-",
        "params": {},
        "prompt": None,
        "response_text": task.remainder_markdown,
        "response_words": len(task.remainder_markdown.split()),
        "target_words": task.target_words,
        "ts": dt.datetime.utcnow().isoformat() + "Z",
        "raw_response": None,
    }
    jsonl.append(log_path, row)
    existing_keys.add(k)


def ensure_paraphrase_rows(
    task: prepare.PreparedTask,
    paraphrase_rows_by_essay_model: dict[tuple[str, str], dict],
    n_paragraphs: int,
    log_path: pathlib.Path,
    existing_keys: set[str],
) -> None:
    """For each cached paraphrase of this essay, derive and persist a paraphrase-remainder row."""
    for (essay_id, model_id), para_row in paraphrase_rows_by_essay_model.items():
        if essay_id != task.essay_id:
            continue
        source_id = f"paraphrase:{model_id}"
        samp_hash = para_row.get("sampling_hash", "-")
        k = completion_key(task.essay_id, task.prefix_config_hash, source_id, samp_hash)
        if k in existing_keys:
            continue
        para_blocks = [versus_essay.Block(**b) for b in para_row["blocks"]]
        remainder_md = prepare.split_paraphrase(para_blocks, n_paragraphs)
        row = {
            "key": k,
            "essay_id": task.essay_id,
            "prefix_config_hash": task.prefix_config_hash,
            "source_id": source_id,
            "source_kind": "paraphrase",
            "model_id": model_id,
            "paraphrase_model_id": model_id,
            "sampling_hash": samp_hash,
            "params": para_row.get("params", {}),
            "prompt": None,
            "response_text": remainder_md,
            "response_words": len(remainder_md.split()),
            "target_words": task.target_words,
            "ts": dt.datetime.utcnow().isoformat() + "Z",
            "raw_response": None,
        }
        jsonl.append(log_path, row)
        existing_keys.add(k)


def load_paraphrases_by_essay_model(paraphrases_log: pathlib.Path) -> dict[tuple[str, str], dict]:
    out: dict[tuple[str, str], dict] = {}
    for row in jsonl.read(paraphrases_log):
        out[(row["essay_id"], row["model_id"])] = row
    return out


def _call_one_completion(task, prompt, m, sh, k, client):
    t0 = time.time()
    resp = openrouter.chat(
        model=m.id,
        messages=[{"role": "user", "content": prompt}],
        temperature=m.temperature,
        max_tokens=m.max_tokens,
        top_p=m.top_p,
        client=client,
    )
    text = openrouter.extract_text(resp)
    return {
        "key": k,
        "essay_id": task.essay_id,
        "prefix_config_hash": task.prefix_config_hash,
        "source_id": m.id,
        "source_kind": "completion",
        "model_id": m.id,
        "sampling_hash": sh,
        "params": m.model_dump(exclude={"id"}),
        "prompt": prompt,
        "response_text": text,
        "response_words": len(text.split()),
        "target_words": task.target_words,
        "ts": dt.datetime.utcnow().isoformat() + "Z",
        "duration_s": round(time.time() - t0, 2),
        "raw_response": resp,
    }


def run(cfg: config.Config, essays: list[versus_essay.Essay]) -> None:
    log = cfg.storage.completions_log
    existing = jsonl.keys(log)
    paraphrase_rows = load_paraphrases_by_essay_model(cfg.storage.paraphrases_log)

    # Collect API tasks, executing the cheap synchronous steps (human + paraphrase-remainder rows) inline.
    tasks_to_run: list = []
    for essay in essays:
        task = prepare.prepare(
            essay,
            n_paragraphs=cfg.prefix.n_paragraphs,
            include_headers=cfg.prefix.include_headers,
            length_tolerance=cfg.completion.length_tolerance,
        )
        ensure_human_baseline(task, log, existing)
        ensure_paraphrase_rows(task, paraphrase_rows, cfg.prefix.n_paragraphs, log, existing)
        prompt = prepare.render_prompt(
            task,
            include_headers=cfg.prefix.include_headers,
            tolerance=cfg.completion.length_tolerance,
        )
        for m in cfg.completion.models:
            sh = sampling_hash(m)
            k = completion_key(task.essay_id, task.prefix_config_hash, m.id, sh)
            if k in existing:
                print(f"[skip] {k}")
                continue
            tasks_to_run.append((task, prompt, m, sh, k))
            existing.add(k)  # reserve

    if not tasks_to_run:
        return
    print(f"[run ] {len(tasks_to_run)} completion calls (concurrency={cfg.concurrency})")
    client = httpx.Client(timeout=600.0)
    try:
        with ThreadPoolExecutor(max_workers=cfg.concurrency) as pool:
            futures = {
                pool.submit(_call_one_completion, t, p, m, sh, k, client): k
                for (t, p, m, sh, k) in tasks_to_run
            }
            for fut in as_completed(futures):
                k = futures[fut]
                try:
                    row = fut.result()
                except Exception as e:
                    print(f"[err ] {k}: {e}")
                    continue
                jsonl.append(log, row)  # main thread; no lock needed
                print(f"[done] {k}")
    finally:
        client.close()
