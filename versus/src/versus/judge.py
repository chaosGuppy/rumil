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
import time
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
from versus import config, jsonl, openrouter
from versus.versions import BLIND_JUDGE_VERSION, JUDGE_PROMPT_VERSION

Order = Literal["ab", "ba"]


def compute_sampling_hash(sampling: dict | None) -> str | None:
    """Short deterministic hash of sampling params for judge_model dedup.

    Sorted-key JSON so key order doesn't fork the hash. Returns None when
    ``sampling`` is None (agent-sdk paths like rumil:ws / rumil:orch have
    no explicit sampling dict -- task 5 handles those with a tool-prompt
    hash instead). 8 hex chars is enough to distinguish temperature /
    max_tokens combos without cluttering the key.

    Why this matters: per CLAUDE.local.md, "if some judgements were made
    at 0 or at 0.2 temp, we want that to be in the data." Without folding
    sampling into the dedup key, a ``--topup`` at a different temperature
    silently no-ops against existing rows judged at the old temperature.
    """
    if sampling is None:
        return None
    blob = json.dumps(sampling, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:8]


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
    judge_model: str,
    order: Order,
) -> str:
    """Deterministic dedup key for one judgment row.

    ``order`` records which orientation the judge saw the pair in:
    ``"ab"`` when the alphabetically-lower source was shown as Continuation
    A, ``"ba"`` when it was shown as Continuation B. Required (no default)
    so callers are forced to thread it through — silently defaulting would
    hide sites that don't compute the order.

    This is the capability slot for future mirror-mode aggregation (emit
    both orders per pair and collapse them on the read side to cancel
    position bias). Today the enumeration loops still emit one task per
    pair, so every new row is either ``"ab"`` or ``"ba"`` but never both.
    """
    lo, hi = sorted([source_a, source_b])
    return f"{essay_id}|{prefix_hash}|{lo}__vs__{hi}|{criterion}|{judge_model}|{order}"


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


_PHASH_TAG_RE = re.compile(r"^p[0-9a-f]{8}$")
_VERSION_TAG_RE = re.compile(r"^v\d+$")
_SHASH_TAG_RE = re.compile(r"^s[0-9a-f]{8}$")
_THASH_TAG_RE = re.compile(r"^t[0-9a-f]{8}$")
_QHASH_TAG_RE = re.compile(r"^q[0-9a-f]{8}$")


def _peel_ts_tag(parts: list[str]) -> list[str]:
    while parts and (
        _SHASH_TAG_RE.match(parts[-1])
        or _THASH_TAG_RE.match(parts[-1])
        or _QHASH_TAG_RE.match(parts[-1])
    ):
        parts = parts[:-1]
    return parts


def parse_judge_model_suffix(judge_model: str) -> tuple[str, str | None, str | None]:
    """Split a judge_model into ``(base, phash, version)``.

    ``phash`` and ``version`` are the ``p<sha8>`` and ``v<N>`` tags if
    present, else None. Trailing ``:t<sha8>`` (tool-prompt hash,
    ws/orch), ``:s<sha8>`` (sampling-params hash, text variants), and
    ``:q<sha8>`` (pair-surface hash, ws/orch) are absorbed silently
    into ``base`` stripping so the rest of the shape stays the same --
    the frontend doesn't render those hashes separately (they exist in
    the dedup key so topups at different sampling params / tool-prompt
    / page-surface edits don't silently no-op; the human-readable
    sampling dict lives on the judgment row, and the tool-prompt text
    lives in-repo). ``:t``, ``:s``, and ``:q`` are peeled symmetrically
    so any trailing ordering is tolerated.
    """
    parts = judge_model.split(":")
    parts = _peel_ts_tag(parts)
    version = None
    if parts and _VERSION_TAG_RE.match(parts[-1]):
        version = parts[-1]
        parts = parts[:-1]
    phash = None
    if parts and _PHASH_TAG_RE.match(parts[-1]):
        phash = parts[-1]
        parts = parts[:-1]
    return ":".join(parts), phash, version


def base_judge_model(judge_model: str) -> str:
    """Strip ``:p<hash>[:v<N>][:t<hash>][:s<hash>][:q<hash>]`` version suffix to recover the raw model id.

    Use wherever downstream code needs to match the judge_model against a
    source_id (e.g. ``paraphrase:<model>``) or render a column header that
    groups across prompt versions. Delegates to
    :func:`parse_judge_model_suffix` so the peel logic is shared.
    """
    return parse_judge_model_suffix(judge_model)[0]


def compute_judge_prompt_hash(dimension: str) -> str:
    """Short hash of the composed judge prompt for ``dimension``.

    Delegates to :func:`rumil.versus_bridge.compute_prompt_hash` so the
    OpenRouter pathway and the ``rumil:text`` / ``rumil:ws`` pathways
    produce the same ``p<hash>`` for the same dimension. Any edit to
    ``versus-judge-shell.md`` or the dimension prompt forks the hash
    naturally.
    """
    body = get_rumil_dimension_body(dimension)
    return compute_prompt_hash(body)


def judge_prompt_is_current(judge_model: str, criterion: str) -> bool:
    """Return False if the row's ``p<hash>:v<N>`` suffix is out of date.

    Catches the staleness class that ``prefix_config_hash`` doesn't: when
    ``versus-judge-shell.md`` / ``versus-<dim>.md`` get edited, or
    ``JUDGE_PROMPT_VERSION`` / ``BLIND_JUDGE_VERSION`` get bumped, previously
    cached judgment rows point at an old prompt. Status.py uses this to
    surface them in the STALE banner; without it, a prompt/version bump
    silently leaves rows that look current.

    Rumil-style judges (``rumil:ws:*``, ``rumil:orch:*``, ``rumil:text:*``)
    gate on ``BLIND_JUDGE_VERSION``; everyone else gates on
    ``JUDGE_PROMPT_VERSION``. We recover dimension from the row's
    ``criterion`` column (passed in here) rather than re-parsing the
    judge_model string, which differs in shape per variant.
    """
    _, phash, version = parse_judge_model_suffix(judge_model)
    if phash is None or version is None:
        # Legacy pre-hash judge_model — predates the dedup-version
        # regime entirely. Flag as stale so the operator can decide to
        # regenerate.
        return False
    try:
        expected_ph = f"p{compute_judge_prompt_hash(criterion)}"
    except ValueError:
        # Dimension prompt was removed; row can't match current set.
        return False
    version_const = (
        BLIND_JUDGE_VERSION if judge_model.startswith("rumil:") else JUDGE_PROMPT_VERSION
    )
    expected_v = f"v{version_const}"
    return phash == expected_ph and version == expected_v


def compose_judge_model(
    base_model: str,
    dimension: str,
    sampling: dict | None = None,
) -> str:
    """Append the judge-prompt version + sampling-hash suffix to ``base_model``.

    Produces ``<base_model>:p<hash>:v<N>[:s<hash>]``. Used for OpenRouter
    bare-model judges and ``anthropic:<model>`` judges so their dedup keys
    fork when the judge prompt or sampling params change. The dimension
    is carried in the ``criterion`` column on the judgment row, so it
    isn't repeated here.

    ``sampling`` is the same dict recorded on the judgment row
    (e.g. ``{"temperature": 0.0, "max_tokens": 8192}``). When provided,
    a deterministic 8-char hash is appended so topups at a different
    temperature re-judge instead of silently no-opping. When None, the
    suffix is omitted (keys predate sampling-hash accounting and shouldn't
    fork retroactively for text variants that didn't use to pass it).
    """
    ph = compute_judge_prompt_hash(dimension)
    base = f"{base_model}:p{ph}:v{JUDGE_PROMPT_VERSION}"
    sh = compute_sampling_hash(sampling)
    if sh is not None:
        base = f"{base}:s{sh}"
    return base


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


def _call_one_judgment(
    essay_id,
    prefix_hash,
    a_id,
    b_id,
    first,
    second,
    criterion,
    base_model,
    judge_model,
    system_prompt,
    user_prompt,
    k,
    order: Order,
    max_tokens,
    client,
):
    t0 = time.time()
    resp = openrouter.chat(
        model=base_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=JUDGE_TEMPERATURE,
        max_tokens=max_tokens,
        client=client,
    )
    text = openrouter.extract_text(resp)
    verdict, preference_label = parse_verdict_from_label(text)
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
        "sampling": {"temperature": JUDGE_TEMPERATURE, "max_tokens": max_tokens},
    }


def run(
    cfg: config.Config,
    *,
    judge_models: list[str] | None = None,
    criteria: list[str] | None = None,
    essay_ids: list[str] | None = None,
    contestants: list[str] | None = None,
    vs_human: bool = False,
    current_only: bool = False,
    limit: int | None = None,
    dry_run: bool = False,
) -> None:
    """Run the OpenRouter pairwise judge matrix against pending rows.

    Filters (all optional, composable):
    - ``judge_models`` -- override ``cfg.judging.models`` (e.g. single judge)
    - ``criteria``     -- override ``cfg.judging.criteria``
    - ``essay_ids``    -- restrict to these essays
    - ``contestants``  -- only pairs where both source_ids are in this list
    - ``vs_human``     -- only pairs where one side is "human"
    - ``current_only`` -- skip groups whose prefix_hash isn't the current
      one for the essay (i.e. they reference older essay markdown). Avoids
      spending on judgments that would immediately be marked stale.
    """
    from versus import prepare

    groups, prefix_texts = load_sources_by_essay(cfg.storage.completions_log)
    existing = jsonl.keys(cfg.storage.judgments_log)

    effective_judges = judge_models if judge_models is not None else cfg.judging.models
    effective_criteria = criteria if criteria is not None else cfg.judging.criteria
    essay_id_set = set(essay_ids) if essay_ids else None
    contestants_set = set(contestants) if contestants else None
    current_hashes = (
        prepare.current_prefix_hashes(cfg, cfg.essays.cache_dir) if current_only else None
    )

    tasks_to_run: list = []
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
            sampling = {
                "temperature": JUDGE_TEMPERATURE,
                "max_tokens": cfg.judging.max_tokens,
            }
            for criterion in effective_criteria:
                for base_model in effective_judges:
                    judge_model = compose_judge_model(base_model, criterion, sampling=sampling)
                    k = judgment_key(
                        essay_id, prefix_hash, a_id, b_id, criterion, judge_model, order
                    )
                    if k in existing:
                        continue
                    system_prompt, user_prompt = render_judge_prompt(
                        prefix_text=prefix_text,
                        dimension=criterion,
                        source_a_text=first.text,
                        source_b_text=second.text,
                    )
                    tasks_to_run.append(
                        (
                            essay_id,
                            prefix_hash,
                            a_id,
                            b_id,
                            first,
                            second,
                            criterion,
                            base_model,
                            judge_model,
                            system_prompt,
                            user_prompt,
                            k,
                            order,
                        )
                    )
                    existing.add(k)

    if limit is not None:
        tasks_to_run = tasks_to_run[:limit]

    if not tasks_to_run:
        print("[info] no pending judgments")
        return
    print(f"[plan] {len(tasks_to_run)} judgment calls (concurrency={cfg.concurrency})")
    if dry_run:
        for t in tasks_to_run[:20]:
            essay_id, _, a_id, b_id, _, _, crit, _, jm, _, _, _, _ = t
            print(f"  * {essay_id} {a_id} vs {b_id} [{crit}] -> {jm}")
        if len(tasks_to_run) > 20:
            print(f"  ... and {len(tasks_to_run) - 20} more")
        return
    client = httpx.Client(timeout=600.0)
    try:
        with ThreadPoolExecutor(max_workers=cfg.concurrency) as pool:
            futures = {
                pool.submit(_call_one_judgment, *t, cfg.judging.max_tokens, client): t[
                    11
                ]  # key is 12th element
                for t in tasks_to_run
            }
            done = 0
            total = len(tasks_to_run)
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
