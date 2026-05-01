"""Run model completions for prepared tasks, with DB-backed dedup."""

from __future__ import annotations

import datetime as dt
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import httpx

from versus import anthropic_client, config, openrouter, prepare, versus_db
from versus import essay as versus_essay
from versus.judge import route_judge_model
from versus.run_summary import RunSummary

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


def build_request_body(
    model_id: str,
    prompt: str,
    *,
    temperature: float | None,
    top_p: float | None,
    max_tokens: int | None,
) -> dict[str, Any]:
    """The canonical 'what we asked for' dict — used for both dedup hashing and storage.

    Provider-shaped (OpenAI/Anthropic-compatible). Independent of the exact
    on-wire bytes the SDK constructs (those vary across SDK versions and
    aren't part of the eval condition).
    """
    body: dict[str, Any] = {
        "model": model_id,
        "messages": [{"role": "user", "content": prompt}],
    }
    if temperature is not None:
        body["temperature"] = temperature
    if top_p is not None:
        body["top_p"] = top_p
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    return body


def _existing_lookup(client) -> set[tuple[str, ...]]:
    """Build a set of dedup keys covering existing texts.

    For human rows: (essay_id, 'human', prefix_hash).
    For completion rows: (essay_id, source_id, request_hash).
    """
    existing: set[tuple[str, ...]] = set()
    for r in versus_db.iter_texts(client):
        if r["kind"] == "human":
            existing.add((r["essay_id"], "human", r["prefix_hash"]))
        elif r.get("request_hash"):
            existing.add((r["essay_id"], r["source_id"], r["request_hash"]))
    return existing


def ensure_human_baseline(
    client,
    task: prepare.PreparedTask,
    existing: set[tuple[str, ...]],
) -> None:
    key = (task.essay_id, "human", task.prefix_config_hash)
    if key in existing:
        return
    versus_db.insert_text(
        client,
        essay_id=task.essay_id,
        kind="human",
        source_id=HUMAN_SOURCE_ID,
        text=task.remainder_markdown,
        prefix_hash=task.prefix_config_hash,
        model_id=None,
        request=None,
        response=None,
        params={"target_words": task.target_words},
    )
    existing.add(key)


def _model_sampling(m: config.ModelCfg, canonical_model: str) -> tuple[float | None, float | None]:
    """Resolve (temperature, top_p) honoring per-provider quirks.

    Opus 4.7 deprecates explicit temperature on Messages (returns 400); top_p
    behaviour isn't documented, so drop both out of caution.
    """
    if canonical_model.startswith("claude-opus-4-7"):
        return None, None
    return m.temperature, m.top_p


_TRANSIENT_HTTPX_ERRORS = (
    httpx.TimeoutException,
    httpx.ReadError,
    httpx.ConnectError,
    httpx.RemoteProtocolError,
)


def _with_transient_retry(fn, *, label: str, max_retries: int = 3, base_delay: float = 2.0):
    """Retry transient errors (5xx, timeouts, connection drops) with exponential backoff.

    base_delay * 2**attempt → 2s, 4s, 8s for attempts 0/1/2. 4xx errors and
    other exceptions are not retried — they're not transient.
    """
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status < 500 or attempt >= max_retries:
                raise
            wait = base_delay * (2**attempt)
            print(
                f"[retry] {label}: {status} "
                f"(attempt {attempt + 1}/{max_retries + 1}, sleeping {wait:.0f}s)"
            )
            time.sleep(wait)
        except _TRANSIENT_HTTPX_ERRORS as e:
            if attempt >= max_retries:
                raise
            wait = base_delay * (2**attempt)
            print(
                f"[retry] {label}: {type(e).__name__} "
                f"(attempt {attempt + 1}/{max_retries + 1}, sleeping {wait:.0f}s)"
            )
            time.sleep(wait)
    raise RuntimeError("unreachable")  # pyright: ignore[reportUnreachable]


def _call_one_completion(
    task,
    prompt,
    m,
    request_body,
    client,
    semaphore: threading.BoundedSemaphore,
):
    with semaphore:
        return _call_one_completion_inner(task, prompt, m, request_body, client)


def _call_one_completion_inner(task, prompt, m, request_body, client):
    t0 = time.time()
    provider, canonical_model = route_judge_model(m.id)
    temp, top_p = _model_sampling(m, canonical_model)

    def _provider_call():
        if provider == "anthropic":
            r = anthropic_client.chat(
                model=canonical_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=temp,
                max_tokens=m.max_tokens,
                top_p=top_p,
                client=client,
            )
            return r, anthropic_client.extract_text(r)
        r = openrouter.chat(
            model=canonical_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=m.temperature,
            max_tokens=m.max_tokens,
            top_p=m.top_p,
            client=client,
        )
        return r, openrouter.extract_text(r)

    resp, raw_text = _with_transient_retry(_provider_call, label=f"{task.essay_id} | {m.id}")
    text = extract_continuation(raw_text)
    return {
        "essay_id": task.essay_id,
        "prefix_hash": task.prefix_config_hash,
        "source_id": m.id,
        "model_id": m.id,
        "request": request_body,
        "response": resp,
        "text": text,
        "params": {
            **m.model_dump(exclude={"id"}),
            "raw_response_text": raw_text,
            "target_words": task.target_words,
            "duration_s": round(time.time() - t0, 2),
            "ts": dt.datetime.utcnow().isoformat() + "Z",
            "provider": provider,
            # Direct anthropic_client doesn't send a thinking block; record
            # explicitly so future callers can tell. See rumil.llm.thinking_config
            # for the rules that apply on bridge paths.
            "thinking": None,
        },
    }


def run(
    cfg: config.Config,
    essays: list[versus_essay.Essay],
    *,
    prefix_cfg: config.PrefixCfg | None = None,
    prod: bool = False,
    dry_run: bool = False,
) -> None:
    pcfg = prefix_cfg if prefix_cfg is not None else cfg.prefix
    db = versus_db.get_client(prod=prod)
    existing = _existing_lookup(db)

    tasks_to_run: list = []
    for essay in essays:
        task = prepare.prepare(
            essay,
            n_paragraphs=pcfg.n_paragraphs,
            include_headers=pcfg.include_headers,
            length_tolerance=cfg.completion.length_tolerance,
        )
        if not dry_run:
            ensure_human_baseline(db, task, existing)
        prompt = prepare.render_prompt(
            task,
            include_headers=pcfg.include_headers,
            tolerance=cfg.completion.length_tolerance,
        )
        for m in cfg.completion.models:
            _, canonical_model = route_judge_model(m.id)
            temp, top_p = _model_sampling(m, canonical_model)
            request_body = build_request_body(
                m.id, prompt, temperature=temp, top_p=top_p, max_tokens=m.max_tokens
            )
            request_hash = versus_db.compute_canonical_hash(request_body)
            key = (task.essay_id, m.id, request_hash)
            if key in existing:
                print(f"[skip] {task.essay_id} | {m.id} | {request_hash[:12]}")
                continue
            tasks_to_run.append((task, prompt, m, request_body))
            existing.add(key)

    if not tasks_to_run:
        return
    if dry_run:
        models_in_plan = sorted({m.id for _, _, m, _ in tasks_to_run})
        print(
            f"[plan] {len(tasks_to_run)} completion calls "
            f"(per_model_concurrency={cfg.per_model_concurrency}, "
            f"models={len(models_in_plan)})"
        )
        for task, _prompt, m, _rb in tasks_to_run:
            print(f"  * {task.essay_id} | {m.id}")
        return
    # Per-model semaphores so a slow reasoning model can't block fast lanes.
    semaphores: dict[str, threading.BoundedSemaphore] = {
        m_id: threading.BoundedSemaphore(cfg.per_model_concurrency)
        for m_id in {m.id for _, _, m, _ in tasks_to_run}
    }
    total_workers = cfg.per_model_concurrency * len(semaphores)
    print(
        f"[run ] {len(tasks_to_run)} completion calls "
        f"(per_model_concurrency={cfg.per_model_concurrency}, "
        f"models={len(semaphores)}, total_workers={total_workers})"
    )
    summary = RunSummary()
    http = httpx.Client(timeout=600.0)
    try:
        with ThreadPoolExecutor(max_workers=total_workers) as pool:
            futures = {
                pool.submit(_call_one_completion, t, p, m, rb, http, semaphores[m.id]): (t, m)
                for (t, p, m, rb) in tasks_to_run
            }
            for fut in as_completed(futures):
                t_essay_id, m = futures[fut][0].essay_id, futures[fut][1]
                try:
                    row = fut.result()
                except Exception as e:
                    print(f"[err ] {t_essay_id} | {m.id}: {e}")
                    summary.record_error()
                    continue
                versus_db.insert_text(
                    db,
                    essay_id=row["essay_id"],
                    kind="completion",
                    source_id=row["source_id"],
                    text=row["text"],
                    prefix_hash=row["prefix_hash"],
                    model_id=row["model_id"],
                    request=row["request"],
                    response=row["response"],
                    params=row["params"],
                )
                summary.record_success(row.get("response"))
                print(f"[done] {row['essay_id']} | {m.id}")
    finally:
        http.close()
        summary.print("completions")
