"""Pairwise blind judging of completions with deterministic ordering.

The blind, ws, and orch judges share a single prompt source-of-truth:
the versus-judge-shell + essay-adapted dimension body live under
``prompts/versus-*.md`` and are loaded via :mod:`rumil.versus_bridge`.
Cross-judge rows are directly comparable on the prompt axis; the
only real difference is model + transport.

Storage: pairwise verdicts land in versus_judgments (Postgres) via
:mod:`versus.versus_db`. Dedup is content-addressed on the
``judge_inputs`` blob, which folds in text_a_id/text_b_id so re-judging
different completion samples naturally forks the hash.
"""

from __future__ import annotations

import hashlib
import itertools
import threading
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from rumil.model_config import ModelConfig
from rumil.versus_prompts import (
    build_system_prompt,
    compute_prompt_hash,
    extract_preference,
    get_rumil_dimension_body,
    label_to_verdict,
)
from versus import anthropic_client, config, openrouter, versus_db
from versus.model_config import get_model_config
from versus.run_summary import RunSummary

Provider = Literal["anthropic", "openrouter"]

Order = Literal["ab", "ba"]


@dataclass
class Source:
    source_id: str  # "human" or the model id
    text: str  # the completion text
    text_id: str  # versus_texts.id — threaded through so judgments can FK to it


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


def order_from_display_first(source_a: str, source_b: str, display_first: str) -> Order:
    """Derive the ``order`` slot from the display-first source id.

    ``"ab"`` iff the alphabetically-lower source is shown as Continuation
    A; ``"ba"`` otherwise. Works for any pair (including ones where
    ``display_first`` equals one of the inputs) -- we only care which
    sorted-slot ended up at position A.
    """
    lo, _ = sorted([source_a, source_b])
    return "ab" if display_first == lo else "ba"


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


def build_blind_judge_config(
    canonical_model: str,
    dimension: str,
    sampling: dict,
    *,
    thinking: dict | None = None,
    effort: str | None = None,
) -> tuple[dict, str, str]:
    """Build ``(config, config_hash, judge_model)`` for one blind judgment.

    Single compose site for the blind path; delegates to
    :func:`versus.judge_config.make_judge_config` so the structured
    config dict and the legacy-shape ``judge_model`` come from one source
    of truth. The returned ``config_hash`` is computed independently of
    DB-side text_a_id/text_b_id; callers fold those in before persisting.

    ``thinking`` and ``effort`` come from the versus model registry —
    blind path now applies them on the wire (via versus.anthropic_client),
    so judge_inputs records what was actually sent. Defaults are None
    for the no-thinking / no-effort case (most non-claude models, plus
    haiku and older sonnet).
    """
    from versus.judge_config import make_judge_config

    return make_judge_config(
        "blind",
        model=canonical_model,
        dimension=dimension,
        sampling=sampling,
        prompt_hash=compute_judge_prompt_hash(dimension, with_tools=False),
        thinking=thinking,
        effort=effort,
    )


def judge_config_is_current(row: dict, criterion: str) -> bool:
    """Return False if any code-side input to the judge has drifted.

    Status.py uses this to surface stale rows in the STALE banner.
    Reads from ``row["judge_inputs"]`` (the canonical condition blob on
    each versus_judgments row).

    Checks the prompt shell hash for all variants, plus the
    ``code_fingerprint`` for ws/orch — that fingerprint catches semantic
    non-prompt edits (parser changes, SDK migrations, etc.) — and both
    the ``thinking`` block and ``effort`` level (None for blind, the
    rules-driven values from ``rumil.llm`` for ws/orch) so changes to
    those rules surface as stale rows. Doesn't check
    ``workspace_state_hash``: that's a per-row baseline watermark, not
    a staleness signal — every row would flap.
    """
    from rumil.llm import effort_level, thinking_config

    cfg = row["judge_inputs"]
    is_tools = cfg["variant"] in ("ws", "orch")
    try:
        expected_ph = compute_judge_prompt_hash(criterion, with_tools=is_tools)
    except ValueError:
        return False
    if cfg["prompts"]["shell_hash"] != expected_ph:
        return False
    if is_tools:
        # circular: rumil.versus_bridge -> versus.judge_config -> versus.judge
        from versus.judge_config import compute_judge_code_fingerprint

        if cfg.get("code_fingerprint") != compute_judge_code_fingerprint():
            return False
    is_blind = cfg["variant"] == "blind"
    expected_thinking = None if is_blind else thinking_config(cfg["model"])
    if cfg.get("thinking") != expected_thinking:
        return False
    expected_effort = None if is_blind else effort_level(cfg["model"])
    if cfg.get("effort") != expected_effort:
        return False
    return True


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
    """Render the (system, user) prompt pair for a blind judge.

    Uses ``versus-judge-shell.md`` + the essay-adapted dimension body
    as the system prompt — identical to what the ws/orch agents see,
    so cross-judge comparisons are apples-to-apples on the prompt
    axis. The user message inlines the essay prefix and both
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
    """True if this versus_texts row ended in a refusal or is too empty to judge.

    Operates on DB-shaped rows: ``kind`` (``human`` / ``completion``),
    ``response`` (raw provider response, may be None), ``text``.

    Human rows are never refusals on their own account — the held-out
    remainder is canonically truth, even if short.

    For completion rows we check:
      - provider-signaled refusal (finish_reason / native_finish_reason), and
      - empty / sub-threshold response text. A model that returned 200 OK
        with two tokens didn't produce a judgable continuation; treating it
        as a contestant feeds noise to the judge.
    """
    if row.get("kind") == "human":
        return False
    rr = row.get("response") or {}
    choices = rr.get("choices") or []
    if choices:
        ch = choices[0] or {}
        fr = ch.get("finish_reason")
        nfr = ch.get("native_finish_reason")
        if fr in REFUSAL_FINISH_REASONS or nfr in REFUSAL_NATIVE_REASONS:
            return True
    text = (row.get("text") or "").strip()
    if not text or len(text.split()) < _MIN_RESPONSE_WORDS:
        return True
    return False


def _prefix_text_from_request(request: dict | None) -> str:
    """Recover the prefix text that was shown in a completion's request body.

    The user message has 'BEGIN ESSAY\\n===\\n...\\n===\\n\\nContinue from here:' —
    grab that slice. Returns empty string if the request is missing or the
    markers don't match (e.g. for human/derived rows with request=None).
    """
    if not request:
        return ""
    messages = request.get("messages") or []
    if not messages:
        return ""
    prompt = (messages[0] or {}).get("content") or ""
    if not isinstance(prompt, str):
        return ""
    start = prompt.find("BEGIN ESSAY\n===\n")
    end = prompt.rfind("\n===\n")
    if start == -1 or end == -1 or end <= start:
        return ""
    return prompt[start + len("BEGIN ESSAY\n===\n") : end].strip()


def load_sources_by_essay(
    client=None,
    *,
    prefix_hash_filter: str | None = None,
    exclude_refusals: bool = True,
) -> tuple[dict[tuple[str, str], dict[str, Source]], dict[tuple[str, str], str]]:
    """Group versus_texts rows by ``(essay_id, prefix_hash)``.

    Returns ``(groups, prefix_text_by_group)`` where ``groups`` maps
    ``(essay_id, prefix_hash)`` to ``{source_id: Source(text, text_id)}``.
    Refused / sub-threshold completions are excluded from the returned
    groups by default so they don't show up in pair enumeration. The
    prefix text is recovered from the request body of any completion row
    in the group (deterministic across samples for the same prefix config).

    Multiple text rows for the same ``(essay_id, prefix_hash, source_id)``
    (intentional replicates, e.g. temperature>0 sampling) are collapsed
    last-row-wins by created_at — pair enumeration uses one canonical
    text per source. Re-running the judge against a *specific* replicate
    is a separate flow that doesn't go through this helper.
    """
    if client is None:
        client = versus_db.get_client()
    groups: dict[tuple[str, str], dict[str, Source]] = {}
    prefix_text_by_group: dict[tuple[str, str], str] = {}
    skipped: list[tuple[str, str]] = []
    for row in versus_db.iter_texts(client):
        prefix_hash = row.get("prefix_hash")
        if prefix_hash is None:
            continue
        if prefix_hash_filter and prefix_hash != prefix_hash_filter:
            continue
        k = (row["essay_id"], prefix_hash)
        if row["source_id"] != "human" and k not in prefix_text_by_group:
            pt = _prefix_text_from_request(row.get("request"))
            if pt:
                prefix_text_by_group[k] = pt
        if exclude_refusals and is_refusal(row):
            skipped.append((row["essay_id"], row["source_id"]))
            continue
        groups.setdefault(k, {})[row["source_id"]] = Source(
            source_id=row["source_id"],
            text=row["text"],
            text_id=row["id"],
        )
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
    text_a_id: str
    text_b_id: str
    dimension: str
    base_model: str
    canonical_model: str
    provider: Provider
    judge_model: str
    system_prompt: str
    user_prompt: str
    order: Order
    sampling: dict
    model_config: ModelConfig
    judge_inputs: dict  # canonical condition blob — hash is judge_inputs_hash
    judge_inputs_hash: str


def _build_judge_inputs(
    base_config: dict,
    text_a_id: str,
    text_b_id: str,
    order: Order,
) -> tuple[dict, str]:
    """Augment a judge_config dict with text id refs + order and compute its hash.

    text_a_id / text_b_id naturally fork the hash when re-judging a
    different completion sample at the same prompt config — so two
    judgments at temp>0 against different completion replicates produce
    distinct rows.
    """
    judge_inputs = dict(base_config)
    judge_inputs["text_a_id"] = text_a_id
    judge_inputs["text_b_id"] = text_b_id
    judge_inputs["order"] = order
    return judge_inputs, versus_db.compute_canonical_hash(judge_inputs)


def _build_judge_request(
    *,
    provider: Provider,
    canonical_model: str,
    system_prompt: str,
    user_prompt: str,
    sampling: dict,
) -> dict[str, Any]:
    """Canonical provider-shaped request body for storage on the row.

    Anthropic carries the system prompt out-of-band; OpenRouter inlines
    it as a leading system message. Either way we record the body the
    eval condition implies (independent of SDK header / version churn).
    """
    body: dict[str, Any] = {"model": canonical_model}
    if provider == "anthropic":
        body["system"] = system_prompt
        body["messages"] = [{"role": "user", "content": user_prompt}]
    else:
        body["messages"] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
    if sampling.get("temperature") is not None:
        body["temperature"] = sampling["temperature"]
    if sampling.get("max_tokens") is not None:
        body["max_tokens"] = sampling["max_tokens"]
    return body


def _call_one_blind(task: _BlindTask, client: httpx.Client) -> dict:
    """Run one blind judgment, dispatching by provider, return DB-shaped row."""
    t0 = time.time()
    mc = task.model_config
    output_cfg = {"effort": mc.effort} if mc.effort is not None else None
    if task.provider == "anthropic":
        resp = anthropic_client.chat(
            model=task.canonical_model,
            system=task.system_prompt,
            messages=[{"role": "user", "content": task.user_prompt}],
            temperature=mc.temperature,
            max_tokens=mc.max_tokens,
            top_p=mc.top_p,
            thinking=mc.thinking,
            output_config=output_cfg,
            client=client,
        )
        text = anthropic_client.extract_text(resp)
    else:
        # OpenRouter path: thinking / output_config aren't supported here;
        # registry entries for OpenRouter-routed judges set them to None.
        resp = openrouter.chat(
            model=task.canonical_model,
            messages=[
                {"role": "system", "content": task.system_prompt},
                {"role": "user", "content": task.user_prompt},
            ],
            temperature=mc.temperature,
            max_tokens=mc.max_tokens,
            top_p=mc.top_p,
            client=client,
        )
        text = openrouter.extract_text(resp)
    verdict, preference_label = parse_verdict_from_label(text)
    request_body = _build_judge_request(
        provider=task.provider,
        canonical_model=task.canonical_model,
        system_prompt=task.system_prompt,
        user_prompt=task.user_prompt,
        sampling=task.sampling,
    )
    return {
        "essay_id": task.essay_id,
        "prefix_hash": task.prefix_hash,
        "source_a": task.a_id,
        "source_b": task.b_id,
        "display_first": task.first.source_id,
        "text_a_id": task.text_a_id,
        "text_b_id": task.text_b_id,
        "criterion": task.dimension,
        "variant": "blind",
        "judge_model": task.judge_model,
        "request": request_body,
        "response": resp,
        "judge_inputs": task.judge_inputs,
        "verdict": verdict,
        "preference_label": preference_label,
        "reasoning_text": text,
        "duration_s": round(time.time() - t0, 2),
    }


def _existing_judgment_keys(client) -> set[tuple[str, ...]]:
    """Build a dedup set keyed on (essay_id, prefix_hash, sa, sb, criterion, judge_inputs_hash)."""
    out: set[tuple[str, ...]] = set()
    for r in versus_db.iter_judgments(client):
        out.add(
            (
                r["essay_id"],
                r["prefix_hash"],
                r["source_a"],
                r["source_b"],
                r["criterion"],
                r["judge_inputs_hash"],
            )
        )
    return out


def run_blind(
    cfg: config.Config,
    *,
    models: Sequence[str],
    dimensions: Sequence[str] | None = None,
    essay_ids: Sequence[str] | None = None,
    contestants: Sequence[str] | None = None,
    vs_human: bool = False,
    current_only: bool = False,
    prefix_cfgs: Sequence[config.PrefixCfg] | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    prod: bool = False,
) -> None:
    """Run pairwise blind judgments across a mixed list of models.

    Each model is routed via :func:`route_judge_model`: claude-* go
    direct to Anthropic, others via OpenRouter. Same prompt
    construction across all (the blind shell — no tool advertisements,
    pair inlined in user message). Dedup is content-addressed on
    ``judge_inputs_hash``: any change to model / sampling / prompt
    content / pair surface auto-forks.
    """
    from versus import prepare

    if not models:
        print("[info] no models passed to run_blind; nothing to do")
        return

    db = versus_db.get_client(prod=prod)
    groups, prefix_texts = load_sources_by_essay(db)
    existing = _existing_judgment_keys(db)

    effective_dimensions = (
        list(dimensions) if dimensions is not None else list(cfg.judging.criteria)
    )
    essay_id_set = set(essay_ids) if essay_ids else None
    contestants_set = set(contestants) if contestants else None
    # Build the union of allowed (essay_id, prefix_hash) pairs across the
    # selected prefix variants. None means "any prefix in versus_texts".
    # current_only=True with no explicit prefix_cfgs falls back to the
    # canonical cfg.prefix variant.
    allowed_prefix_pairs: set[tuple[str, str]] | None
    if prefix_cfgs:
        allowed_prefix_pairs = set()
        for pc in prefix_cfgs:
            for eid, ph in prepare.current_prefix_hashes(cfg, prefix_cfg=pc, client=db).items():
                allowed_prefix_pairs.add((eid, ph))
    elif current_only:
        allowed_prefix_pairs = {
            (eid, ph) for eid, ph in prepare.current_prefix_hashes(cfg, client=db).items()
        }
    else:
        allowed_prefix_pairs = None

    tasks: list[_BlindTask] = []
    for (essay_id, prefix_hash), sources in groups.items():
        if essay_id_set is not None and essay_id not in essay_id_set:
            continue
        if allowed_prefix_pairs is not None and (essay_id, prefix_hash) not in allowed_prefix_pairs:
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
            first, second = order_pair(essay_id, src_a, src_b)
            order = order_from_display_first(a_id, b_id, first.source_id)
            for dimension in effective_dimensions:
                for base_model in models:
                    provider, canonical_model = route_judge_model(base_model)
                    mc = get_model_config(base_model, cfg=cfg)
                    sampling = {
                        "temperature": mc.temperature,
                        "max_tokens": mc.max_tokens,
                    }
                    base_config, _, judge_model = build_blind_judge_config(
                        canonical_model,
                        dimension,
                        sampling,
                        thinking=mc.thinking,
                        effort=mc.effort,
                    )
                    judge_inputs, judge_inputs_hash = _build_judge_inputs(
                        base_config, src_a.text_id, src_b.text_id, order
                    )
                    dedup_key = (
                        essay_id,
                        prefix_hash,
                        a_id,
                        b_id,
                        dimension,
                        judge_inputs_hash,
                    )
                    if dedup_key in existing:
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
                            text_a_id=src_a.text_id,
                            text_b_id=src_b.text_id,
                            dimension=dimension,
                            base_model=base_model,
                            canonical_model=canonical_model,
                            provider=provider,
                            judge_model=judge_model,
                            system_prompt=system_prompt,
                            user_prompt=user_prompt,
                            order=order,
                            sampling=sampling,
                            model_config=mc,
                            judge_inputs=judge_inputs,
                            judge_inputs_hash=judge_inputs_hash,
                        )
                    )
                    existing.add(dedup_key)

    if limit is not None:
        tasks = tasks[:limit]
    if not tasks:
        print("[info] no pending blind judgments")
        return

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

    summary = RunSummary()
    http = httpx.Client(timeout=600.0)
    try:
        with ThreadPoolExecutor(max_workers=total_workers) as pool:
            futures = {
                pool.submit(
                    _call_with_semaphore, semaphores[t.canonical_model], _call_one_blind, t, http
                ): t
                for t in tasks
            }
            done = 0
            total = len(tasks)
            for fut in as_completed(futures):
                t = futures[fut]
                try:
                    row = fut.result()
                except Exception as e:
                    print(f"[err ] {t.essay_id} {t.a_id} vs {t.b_id} [{t.dimension}]: {e}")
                    summary.record_error()
                    continue
                versus_db.insert_judgment(
                    db,
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
                    request=row["request"],
                    response=row["response"],
                    preference_label=row["preference_label"],
                    duration_s=row["duration_s"],
                )
                done += 1
                summary.record_success(row.get("response"))
                print(
                    f"[done {done}/{total}] {t.essay_id} {t.a_id} vs {t.b_id} "
                    f"[{t.dimension}] verdict={row['verdict']}"
                )
    finally:
        http.close()
        summary.print("blind judgments")


def _call_with_semaphore(sem: threading.BoundedSemaphore, fn, *args, **kwargs):
    with sem:
        return fn(*args, **kwargs)
