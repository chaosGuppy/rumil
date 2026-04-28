"""Pairwise blind judging of completions with deterministic ordering.

OpenRouter and ``anthropic:<model>`` (text-mode) judges share the same
prompt source-of-truth as ``rumil:text`` / ``rumil:ws`` / ``rumil:orch``
-- the versus-judge-shell + the essay-adapted dimension body live in
``prompts/versus-*.md`` and are loaded via :mod:`rumil.versus_bridge`.
This means cross-judge rows are directly comparable on the prompt axis;
the only real difference is the model + transport.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import itertools
import json
import pathlib
import re
import threading
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Literal

import httpx

from rumil.versus_prompts import (
    build_system_prompt,
    compute_prompt_hash,
    extract_preference,
    get_rumil_dimension_body,
    label_to_verdict,
)
from versus import anthropic_client, config, jsonl, openrouter
from versus.versions import BLIND_JUDGE_VERSION

Provider = Literal["anthropic", "openrouter"]

Order = Literal["ab", "ba"]


# Re-export from versus.judge_config — moved there to break the
# lazy-import workaround that judge_config.py used to dodge a cycle.
# Kept here so back-compat callers (analyze, mainline, etc.) don't break.
from versus.judge_config import compute_sampling_hash  # noqa: E402, F401


@dataclass
class Source:
    source_id: str  # "human" or the model id
    text: str  # the completion text


def pair_order_seed(essay_id: str, a: str, b: str) -> int:
    lo, hi = sorted([a, b])
    h = hashlib.sha256(f"{essay_id}|{lo}|{hi}".encode()).hexdigest()
    return int(h[:8], 16)


def order_pair(essay_id: str, a: Source, b: Source) -> tuple[Source, Source]:
    """Return sources in deterministic display order (A, B) for this essay+pair."""
    lo, hi = sorted([a.source_id, b.source_id])
    lo_src = a if a.source_id == lo else b
    hi_src = b if b.source_id == hi else a
    if pair_order_seed(essay_id, lo, hi) % 2 == 0:
        return lo_src, hi_src
    else:
        return hi_src, lo_src


def judgment_key(
    essay_id: str,
    prefix_hash: str,
    source_a: str,
    source_b: str,
    criterion: str,
    config_hash: str,
    order: Order,
) -> str:
    """Deterministic dedup key for one judgment row.

    Keyed on the structured ``config_hash`` (introduced by
    :mod:`versus.judge_config`). Any input the judge saw — model,
    sampling, prompt content, tool descriptions, pair surface, closer
    config, code fingerprint, workspace contents — is folded into
    ``config_hash``, so a change in any of them auto-forks the dedup
    key without anyone having to edit a parser.

    ``order`` records which orientation the judge saw the pair in:
    ``"ab"`` when the alphabetically-lower source was shown as
    Continuation A, ``"ba"`` when it was shown as Continuation B.
    Required (no default) so callers are forced to thread it through.
    Capability slot for future mirror-mode aggregation.
    """
    lo, hi = sorted([source_a, source_b])
    return f"{essay_id}|{prefix_hash}|{lo}__vs__{hi}|{criterion}|{config_hash}|{order}"


def order_from_display_first(source_a: str, source_b: str, display_first: str) -> Order:
    """Derive the ``order`` slot from the display-first source id.

    ``"ab"`` iff the alphabetically-lower source is shown as Continuation
    A; ``"ba"`` otherwise. Works for any pair (including ones where
    ``display_first`` equals one of the inputs) -- we only care which
    sorted-slot ended up at position A.
    """
    lo, _ = sorted([source_a, source_b])
    return "ab" if display_first == lo else "ba"


def infer_order(row: dict) -> Order:
    """Return ``row['order']`` if present; otherwise derive it from ``display_first``.

    Legacy rows (written before the order field was added) don't carry
    ``order``; their orientation is still deterministic and recoverable
    from ``display_first`` vs ``sorted([source_a, source_b])``. Use this
    wherever downstream code needs the per-row order -- it tolerates the
    mix of pre- and post-change rows that will coexist in the judgments
    log.
    """
    existing = row.get("order")
    if existing in ("ab", "ba"):
        return existing  # pyright: ignore[reportReturnType]
    return order_from_display_first(row["source_a"], row["source_b"], row["display_first"])


def compute_judge_prompt_hash(dimension: str, *, with_tools: bool = False) -> str:
    """Short hash of the composed judge prompt for ``dimension``.

    Delegates to :func:`rumil.versus_prompts.compute_prompt_hash`. ``with_tools``
    distinguishes the blind shell (default) from the ws/orch shell so each
    variant tracks its own hash space.
    """
    body = get_rumil_dimension_body(dimension)
    return compute_prompt_hash(body, with_tools=with_tools)


def route_judge_model(model: str) -> tuple[Provider, str]:
    """Return (provider, canonical_id) for a judge model.

    Claude models (with or without ``anthropic/`` prefix) route direct to
    Anthropic. Everything else routes via OpenRouter. The canonical id is
    the post-routing form: bare ``claude-...`` for Anthropic, OR id as-given
    for OpenRouter — so ``anthropic/claude-opus-4-7`` and ``claude-opus-4-7``
    canonicalise to the same key.
    """
    bare = model.removeprefix("anthropic/")
    if bare.startswith("claude-"):
        return ("anthropic", bare)
    return ("openrouter", model)


def _sampling_for(provider: Provider, canonical_model: str, max_tokens: int) -> dict:
    """Per-provider sampling defaults.

    Anthropic-direct: temperature is omitted for opus 4.7 (the Messages API
    deprecated the param and 400s on it); 0.0 elsewhere. OpenRouter: always
    0.0 (existing behaviour). Folded into the sampling-hash so topups at a
    different temp re-judge instead of silently no-opping.
    """
    if provider == "anthropic":
        use_temp = None if canonical_model.startswith("claude-opus-4-7") else 0.0
        return {"temperature": use_temp, "max_tokens": max_tokens}
    return {"temperature": JUDGE_TEMPERATURE, "max_tokens": max_tokens}


def compose_blind_judge_model(canonical_model: str, dimension: str, sampling: dict) -> str:
    """Build a blind judge_model dedup key.

    Shape: ``<canonical_model>:<dimension>:p<phash>:v<BLIND_JUDGE_VERSION>:s<shash>``.
    Single shape across providers — model id alone disambiguates route at
    parse time. ``rumil:ws:`` / ``rumil:orch:`` keep their own prefixes.

    Thin wrapper around :func:`build_blind_judge_config` for back-compat
    with callers that only need the flat string. New code paths should
    call ``build_blind_judge_config`` directly so they get the
    structured ``config`` + ``config_hash`` to write onto each row.
    """
    _, _, judge_model = build_blind_judge_config(canonical_model, dimension, sampling)
    return judge_model


def build_blind_judge_config(
    canonical_model: str, dimension: str, sampling: dict
) -> tuple[dict, str, str]:
    """Build ``(config, config_hash, judge_model)`` for one blind judgment.

    Single compose site for the blind path; delegates to
    :func:`versus.judge_config.make_judge_config` so the structured
    config dict and the legacy-shape ``judge_model`` come from one source
    of truth. Callers persist all three on the row.
    """
    from versus.judge_config import make_judge_config
    from versus.versions import COMPLETION_PROMPT_VERSION

    return make_judge_config(
        "blind",
        model=canonical_model,
        dimension=dimension,
        sampling=sampling,
        blind_judge_version=BLIND_JUDGE_VERSION,
        completion_prompt_version=COMPLETION_PROMPT_VERSION,
        prompt_hash=compute_judge_prompt_hash(dimension, with_tools=False),
    )


def judge_prompt_is_current(row_or_jm: dict | str, criterion: str) -> bool:
    """Return False if the row's prompt hash / version is out of date.

    Status.py uses this to surface stale rows in the STALE banner.
    Accepts either a full row dict (preferred — reads
    ``row["config"]["prompts"]`` directly) or the bare flat
    ``judge_model`` string (legacy callers).

    Tools-mode keys (``rumil:ws:*``, ``rumil:orch:*``) hash the tools-
    shell composed output; blind keys hash the blind-shell composed
    output. Legacy unversioned keys read as stale by construction.
    """
    cfg: dict | None = None
    judge_model: str
    if isinstance(row_or_jm, dict):
        c = row_or_jm.get("config")
        if isinstance(c, dict):
            cfg = c
        judge_model = str(row_or_jm.get("judge_model", ""))
    else:
        judge_model = row_or_jm
    if cfg is not None:
        phash = f"p{cfg['prompts']['shell_hash']}"
        version = f"v{cfg['prompts']['blind_judge_version']}"
        is_tools = cfg["variant"] in ("ws", "orch")
    else:
        _, phash, version = parse_judge_model_suffix(judge_model)
        if phash is None or version is None:
            return False
        is_tools = judge_model.startswith(("rumil:ws:", "rumil:orch:"))
    try:
        expected_ph = f"p{compute_judge_prompt_hash(criterion, with_tools=is_tools)}"
    except ValueError:
        return False
    return phash == expected_ph and version == f"v{BLIND_JUDGE_VERSION}"


def parse_verdict_from_label(text: str) -> tuple[str | None, str | None]:
    """Return ``(verdict, preference_label)`` parsed from a judge response.

    Looks for one of the seven preference labels defined in
    :mod:`rumil.versus_bridge` and maps it to a 3-way verdict
    (``A`` / ``B`` / ``tie``). Returns ``(None, None)`` if no label
    is found in the text.
    """
    label = extract_preference(text)
    return label_to_verdict(label), label


def render_judge_prompt(
    prefix_text: str,
    dimension: str,
    source_a_text: str,
    source_b_text: str,
) -> tuple[str, str]:
    """Render the (system, user) prompt pair for a text-mode judge.

    Uses ``versus-judge-shell.md`` + the essay-adapted dimension body as
    the system prompt -- identical to what ``rumil:text`` (and the agent
    variants) see, so cross-judge comparisons are apples-to-apples on the
    prompt axis. The user message inlines the essay prefix and both
    continuations; no source ids are disclosed.
    """
    body = get_rumil_dimension_body(dimension)
    system = build_system_prompt(body)
    user = (
        "Compare Continuation A and Continuation B on the dimension "
        f"**{dimension}**.\n\n"
        "End your response with one of the 7-point preference labels "
        "on its own line.\n\n"
        f"## Essay opening\n\n{prefix_text}\n\n"
        f"## Continuation A\n\n{source_a_text}\n\n"
        f"## Continuation B\n\n{source_b_text}\n"
    )
    return system, user


REFUSAL_NATIVE_REASONS = {"refusal", "content_filter", "safety", "blocked"}
REFUSAL_FINISH_REASONS = {"content_filter"}

_MIN_RESPONSE_WORDS = 10


def is_refusal(row: dict) -> bool:
    """True if this completion row ended in a refusal or is too empty to judge.

    Human baseline and paraphrase-remainder rows set raw_response=None and
    write response_text directly (the held-out remainder or the derived
    paraphrase remainder). They're never refusals on their own account;
    delegated-refusal state for paraphrase rows is carried explicitly on
    a top-level ``paraphrase_refusal`` flag set by ``ensure_paraphrase_rows``
    when the upstream paraphrase was itself refused.

    For model-completion rows, we check:
      - provider-signaled refusal (finish_reason / native_finish_reason), and
      - empty / sub-threshold response text. A model that returned 200 OK with
        two tokens didn't produce a judgable continuation; treating it as a
        contestant would feed noise into the judge. Threshold is low enough
        that legitimately short continuations pass; anything under it is
        effectively non-response.
    """
    if row.get("paraphrase_refusal"):
        return True
    rr = row.get("raw_response") or {}
    choices = rr.get("choices") or []
    if choices:
        ch = choices[0] or {}
        fr = ch.get("finish_reason")
        nfr = ch.get("native_finish_reason")
        if fr in REFUSAL_FINISH_REASONS or nfr in REFUSAL_NATIVE_REASONS:
            return True
    # Only flag empty responses for rows that actually came from a model
    # (source_kind=completion). Human / paraphrase-remainder rows are
    # derived text and don't carry provider state.
    if row.get("source_kind") == "completion":
        text = (row.get("response_text") or "").strip()
        if not text or len(text.split()) < _MIN_RESPONSE_WORDS:
            return True
    return False


def _prefix_text_from_completion_row(row: dict) -> str:
    """Recover the prefix text that was shown in the completion prompt.

    The prompt has 'BEGIN ESSAY\\n===\\n...\\n===\\n\\nContinue from here:' — grab that slice.
    Falls back to an empty string if we can't find the markers (human rows have prompt=None).
    """
    prompt = row.get("prompt") or ""
    if not prompt:
        return ""
    start = prompt.find("BEGIN ESSAY\n===\n")
    end = prompt.rfind("\n===\n")
    if start == -1 or end == -1 or end <= start:
        return ""
    return prompt[start + len("BEGIN ESSAY\n===\n") : end].strip()


def load_sources_by_essay(
    log_path: pathlib.Path,
    prefix_hash_filter: str | None = None,
    *,
    exclude_refusals: bool = True,
):
    """Group completion rows by (essay_id, prefix_config_hash).

    Refused / content-filtered completions are excluded from the returned groups by
    default so they don't show up in pair enumeration for judging. Prefix-text
    recovery still uses the row (prefix text is unaffected by refusal).
    """
    groups: dict[tuple[str, str], dict] = {}
    prefix_text_by_group: dict[tuple[str, str], str] = {}
    skipped: list[tuple[str, str]] = []
    for row in jsonl.read(log_path):
        k = (row["essay_id"], row["prefix_config_hash"])
        if prefix_hash_filter and row["prefix_config_hash"] != prefix_hash_filter:
            continue
        if row["source_id"] != "human" and k not in prefix_text_by_group:
            pt = _prefix_text_from_completion_row(row)
            if pt:
                prefix_text_by_group[k] = pt
        if exclude_refusals and is_refusal(row):
            skipped.append((row["essay_id"], row["source_id"]))
            continue
        groups.setdefault(k, {})[row["source_id"]] = row["response_text"]
    if skipped:
        for essay_id, source_id in skipped:
            print(f"[skip-refusal] {essay_id} / {source_id}")
    return groups, prefix_text_by_group


JUDGE_TEMPERATURE = 0.0


@dataclass(frozen=True)
class _BlindTask:
    essay_id: str
    prefix_hash: str
    a_id: str
    b_id: str
    first: Source
    second: Source
    dimension: str
    base_model: str  # as the user typed it (alias resolved, anthropic/ prefix retained if given)
    canonical_model: str  # post-routing id
    provider: Provider
    judge_model: str
    system_prompt: str
    user_prompt: str
    key: str
    order: Order
    sampling: dict
    # Structured config + its sha256 — written onto the row alongside
    # ``judge_model`` so downstream provenance reads from one place
    # instead of regex-parsing the flat string.
    config: dict
    config_hash: str


def _call_one_blind(task: _BlindTask, client: httpx.Client) -> dict:
    """Run one blind judgment, dispatching by provider."""
    t0 = time.time()
    if task.provider == "anthropic":
        resp = anthropic_client.chat(
            model=task.canonical_model,
            system=task.system_prompt,
            messages=[{"role": "user", "content": task.user_prompt}],
            temperature=task.sampling["temperature"],
            max_tokens=task.sampling["max_tokens"],
            client=client,
        )
        text = anthropic_client.extract_text(resp)
    else:
        resp = openrouter.chat(
            model=task.canonical_model,
            messages=[
                {"role": "system", "content": task.system_prompt},
                {"role": "user", "content": task.user_prompt},
            ],
            temperature=task.sampling["temperature"],
            max_tokens=task.sampling["max_tokens"],
            client=client,
        )
        text = openrouter.extract_text(resp)
    verdict, preference_label = parse_verdict_from_label(text)
    winner_source = None
    if verdict == "A":
        winner_source = task.first.source_id
    elif verdict == "B":
        winner_source = task.second.source_id
    elif verdict == "tie":
        winner_source = "tie"
    return {
        "key": task.key,
        "essay_id": task.essay_id,
        "prefix_config_hash": task.prefix_hash,
        "source_a": task.a_id,
        "source_b": task.b_id,
        "display_first": task.first.source_id,
        "display_second": task.second.source_id,
        "order": task.order,
        "criterion": task.dimension,
        "judge_model": task.judge_model,
        "verdict": verdict,
        "winner_source": winner_source,
        "preference_label": preference_label,
        "reasoning_text": text,
        "prompt": task.user_prompt,
        "system_prompt": task.system_prompt,
        "ts": dt.datetime.utcnow().isoformat() + "Z",
        "duration_s": round(time.time() - t0, 2),
        "raw_response": resp,
        "sampling": task.sampling,
        "provider": task.provider,
        "config": task.config,
        "config_hash": task.config_hash,
    }


def run_blind(
    cfg: config.Config,
    *,
    models: Sequence[str],
    dimensions: Sequence[str] | None = None,
    essay_ids: Sequence[str] | None = None,
    contestants: Sequence[str] | None = None,
    vs_human: bool = False,
    current_only: bool = False,
    prefix_cfg: config.PrefixCfg | None = None,
    limit: int | None = None,
    dry_run: bool = False,
) -> None:
    """Run pairwise blind judgments across a mixed list of models.

    Each model is routed via :func:`route_judge_model`: claude-* go direct
    to Anthropic, others via OpenRouter. Same prompt construction across
    all (the blind shell — no tool advertisements, pair inlined in user
    message). Single key shape per row:
    ``<canonical_model>:<dimension>:p<phash>:v<BLIND_JUDGE_VERSION>:s<shash>``.
    """
    from versus import prepare

    if not models:
        print("[info] no models passed to run_blind; nothing to do")
        return

    groups, prefix_texts = load_sources_by_essay(cfg.storage.completions_log)
    existing = jsonl.keys(cfg.storage.judgments_log)

    effective_dimensions = (
        list(dimensions) if dimensions is not None else list(cfg.judging.criteria)
    )
    essay_id_set = set(essay_ids) if essay_ids else None
    contestants_set = set(contestants) if contestants else None
    current_hashes = (
        prepare.current_prefix_hashes(cfg, cfg.essays.cache_dir, prefix_cfg=prefix_cfg)
        if current_only
        else None
    )

    tasks: list[_BlindTask] = []
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
            src_a = Source(a_id, sources[a_id])
            src_b = Source(b_id, sources[b_id])
            first, second = order_pair(essay_id, src_a, src_b)
            order = order_from_display_first(a_id, b_id, first.source_id)
            for dimension in effective_dimensions:
                for base_model in models:
                    provider, canonical_model = route_judge_model(base_model)
                    sampling = _sampling_for(provider, canonical_model, cfg.judging.max_tokens)
                    config_dict, config_hash, judge_model = build_blind_judge_config(
                        canonical_model, dimension, sampling
                    )
                    k = judgment_key(
                        essay_id, prefix_hash, a_id, b_id, dimension, config_hash, order
                    )
                    if k in existing:
                        continue
                    system_prompt, user_prompt = render_judge_prompt(
                        prefix_text=prefix_text,
                        dimension=dimension,
                        source_a_text=first.text,
                        source_b_text=second.text,
                    )
                    tasks.append(
                        _BlindTask(
                            essay_id=essay_id,
                            prefix_hash=prefix_hash,
                            a_id=a_id,
                            b_id=b_id,
                            first=first,
                            second=second,
                            dimension=dimension,
                            base_model=base_model,
                            canonical_model=canonical_model,
                            provider=provider,
                            judge_model=judge_model,
                            system_prompt=system_prompt,
                            user_prompt=user_prompt,
                            key=k,
                            order=order,
                            sampling=sampling,
                            config=config_dict,
                            config_hash=config_hash,
                        )
                    )
                    existing.add(k)

    if limit is not None:
        tasks = tasks[:limit]
    if not tasks:
        print("[info] no pending blind judgments")
        return

    # Per-canonical-model semaphores so a slow reasoning judge can't starve fast ones.
    semaphores: dict[str, threading.BoundedSemaphore] = {
        cm: threading.BoundedSemaphore(cfg.per_model_concurrency)
        for cm in {t.canonical_model for t in tasks}
    }
    total_workers = cfg.per_model_concurrency * len(semaphores)
    print(
        f"[plan] {len(tasks)} blind judgment calls "
        f"(per_model_concurrency={cfg.per_model_concurrency}, "
        f"judges={len(semaphores)}, total_workers={total_workers})"
    )
    if dry_run:
        for t in tasks[:20]:
            print(f"  * {t.essay_id} {t.a_id} vs {t.b_id} [{t.dimension}] -> {t.judge_model}")
        if len(tasks) > 20:
            print(f"  ... and {len(tasks) - 20} more")
        return

    client = httpx.Client(timeout=600.0)
    try:
        with ThreadPoolExecutor(max_workers=total_workers) as pool:
            futures = {
                pool.submit(
                    _call_with_semaphore, semaphores[t.canonical_model], _call_one_blind, t, client
                ): t.key
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


def _call_with_semaphore(sem: threading.BoundedSemaphore, fn, *args, **kwargs):
    with sem:
        return fn(*args, **kwargs)
