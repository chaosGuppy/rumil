"""Run model completions for prepared tasks, with dedup caching."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

from versus import anthropic_client, config, jsonl, openrouter, prepare
from versus import essay as versus_essay
from versus.judge import REFUSAL_FINISH_REASONS, REFUSAL_NATIVE_REASONS, route_judge_model

HUMAN_SOURCE_ID = "human"

_CONTINUATION_RE = re.compile(r"<continuation>(.*?)</continuation>", re.DOTALL | re.IGNORECASE)


def extract_continuation(text: str) -> str:
    """Return the final ``<continuation>...</continuation>`` block if present.

    Models may emit scratch space (outlines, dead-ends, planning) before the
    tagged block; we keep only what's inside the last tag. Falls back to the
    full response when the model omits the tag entirely so downstream code
    never gets an empty string from a well-formed-but-untagged continuation.
    """
    matches = _CONTINUATION_RE.findall(text)
    if matches:
        return matches[-1].strip()
    return text.strip()


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
        # Carry the upstream paraphrase's refusal state onto the derived
        # remainder row. Without this, a refused paraphrase would surface
        # as a contestant (the derived row has raw_response=None, so
        # is_refusal can't recover the state on its own) and feed garbage
        # text to the judge.
        parent_refused = _paraphrase_was_refused(para_row)
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
            "paraphrase_refusal": parent_refused,
        }
        jsonl.append(log_path, row)
        existing_keys.add(k)


def _paraphrase_was_refused(para_row: dict) -> bool:
    """Mirror of judge.is_refusal for paraphrase-log rows.

    Paraphrase rows don't carry ``source_kind`` (they aren't contestants
    themselves), so the completion-shaped is_refusal path skips the
    empty-text check on them. Apply the same semantics explicitly here:
    provider-flagged refusal OR an output too thin to be a usable
    paraphrase.
    """
    rr = para_row.get("raw_response") or {}
    for ch in rr.get("choices") or []:
        fr = (ch or {}).get("finish_reason")
        nfr = (ch or {}).get("native_finish_reason")
        if fr in REFUSAL_FINISH_REASONS or nfr in REFUSAL_NATIVE_REASONS:
            return True
        break
    text = (para_row.get("response_text") or "").strip()
    return not text or len(text.split()) < 50


def load_paraphrases_by_essay_model(paraphrases_log: pathlib.Path) -> dict[tuple[str, str], dict]:
    out: dict[tuple[str, str], dict] = {}
    for row in jsonl.read(paraphrases_log):
        out[(row["essay_id"], row["model_id"])] = row
    return out


def _call_one_completion(task, prompt, m, sh, k, client, semaphore: threading.BoundedSemaphore):
    with semaphore:
        return _call_one_completion_inner(task, prompt, m, sh, k, client)


def _call_one_completion_inner(task, prompt, m, sh, k, client):
    t0 = time.time()
    provider, canonical_model = route_judge_model(m.id)
    if provider == "anthropic":
        # Opus 4.7 deprecates explicit temperature on Messages (returns 400). top_p
        # behaviour on opus 4.7 isn't documented; drop it too out of caution. If a
        # caller really wants top_p on opus, revisit with a doc-confirmed test.
        if canonical_model.startswith("claude-opus-4-7"):
            temp, top_p = None, None
        else:
            temp, top_p = m.temperature, m.top_p
        resp = anthropic_client.chat(
            model=canonical_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=temp,
            max_tokens=m.max_tokens,
            top_p=top_p,
            client=client,
        )
        raw_text = anthropic_client.extract_text(resp)
    else:
        resp = openrouter.chat(
            model=canonical_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=m.temperature,
            max_tokens=m.max_tokens,
            top_p=m.top_p,
            client=client,
        )
        raw_text = openrouter.extract_text(resp)
    text = extract_continuation(raw_text)
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
        "raw_response_text": raw_text,
        "target_words": task.target_words,
        "ts": dt.datetime.utcnow().isoformat() + "Z",
        "duration_s": round(time.time() - t0, 2),
        "raw_response": resp,
        "provider": provider,
    }


def run(
    cfg: config.Config,
    essays: list[versus_essay.Essay],
    *,
    prefix_cfg: config.PrefixCfg | None = None,
) -> None:
    pcfg = prefix_cfg if prefix_cfg is not None else cfg.prefix
    log = cfg.storage.completions_log
    existing = jsonl.keys(log)
    paraphrase_rows = load_paraphrases_by_essay_model(cfg.storage.paraphrases_log)

    # Collect API tasks, executing the cheap synchronous steps (human + paraphrase-remainder rows) inline.
    tasks_to_run: list = []
    for essay in essays:
        task = prepare.prepare(
            essay,
            n_paragraphs=pcfg.n_paragraphs,
            include_headers=pcfg.include_headers,
            length_tolerance=cfg.completion.length_tolerance,
        )
        ensure_human_baseline(task, log, existing)
        ensure_paraphrase_rows(task, paraphrase_rows, pcfg.n_paragraphs, log, existing)
        prompt = prepare.render_prompt(
            task,
            include_headers=pcfg.include_headers,
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
    # Per-model semaphores so a slow reasoning model can't block fast
    # lanes. Sized at cfg.per_model_concurrency each; total in-flight
    # calls = per_model_concurrency × number of distinct models.
    semaphores: dict[str, threading.BoundedSemaphore] = {
        m_id: threading.BoundedSemaphore(cfg.per_model_concurrency)
        for m_id in {m.id for _, _, m, _, _ in tasks_to_run}
    }
    total_workers = cfg.per_model_concurrency * len(semaphores)
    print(
        f"[run ] {len(tasks_to_run)} completion calls "
        f"(per_model_concurrency={cfg.per_model_concurrency}, "
        f"models={len(semaphores)}, total_workers={total_workers})"
    )
    client = httpx.Client(timeout=600.0)
    try:
        with ThreadPoolExecutor(max_workers=total_workers) as pool:
            futures = {
                pool.submit(_call_one_completion, t, p, m, sh, k, client, semaphores[m.id]): k
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
