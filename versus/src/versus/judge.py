"""Pairwise blind judging of completions with deterministic ordering."""

from __future__ import annotations

import datetime as dt
import hashlib
import itertools
import pathlib
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import httpx

from versus import config, jsonl, openrouter

CRITERION_PROMPTS: dict[str, str] = {
    "standalone_quality": (
        "Judge on standalone quality. Which continuation reads as better, clearer,"
        " better-argued writing? Reward precise thinking, interesting reasoning, and"
        " prose that rewards careful reading. Penalize vagueness, padding, and"
        " tangents. You are not told which (if any) is the original; judge blindly on"
        " merit."
    ),
    "informativeness": (
        "Which continuation is more informative, all things considered? Reward pieces"
        " that leave the reader with more usable knowledge, frameworks, distinctions,"
        " evidence, or concrete examples."
    ),
    "substance_and_bite": (
        "Great essays on topics like this do real intellectual work: they take"
        " positions, make concrete non-obvious claims, engage with real objections,"
        " and produce frameworks or conclusions a reader can carry forward. Excessive"
        " hedging, surface-level points, and vibes-only prose are weaker even if"
        " stylish. By that standard, which continuation is better?"
    ),
}


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
) -> str:
    lo, hi = sorted([source_a, source_b])
    return f"{essay_id}|{prefix_hash}|{lo}__vs__{hi}|{criterion}|{judge_model}"


VERDICT_RE = re.compile(r"<\s*verdict\s*>\s*(A|B|tie)\s*<\s*/\s*verdict\s*>", re.IGNORECASE)


def parse_verdict(text: str) -> str | None:
    matches = VERDICT_RE.findall(text)
    if not matches:
        return None
    v = matches[-1].lower()
    return "tie" if v == "tie" else v.upper()


def render_judge_prompt(
    prefix_text: str,
    criterion: str,
    source_a_text: str,
    source_b_text: str,
) -> str:
    criterion_desc = CRITERION_PROMPTS[criterion]
    return f"""You are a blind judge comparing two continuations of the same essay opening.

CRITERION
{criterion_desc}

ESSAY OPENING (for context)
===
{prefix_text}
===

CONTINUATION A
===
{source_a_text}
===

CONTINUATION B
===
{source_b_text}
===

Think it through in as much detail as you want: weigh specific passages, note strengths and weaknesses of each, and reach a considered judgment. When you are done reasoning, end your response with exactly one of these tags on its own line:

<verdict>A</verdict>   (A is better)
<verdict>B</verdict>   (B is better)
<verdict>tie</verdict> (a genuine tie or too close to call)

Only the last such tag in your response is read; feel free to revise as you think."""


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


def load_sources_by_essay(log_path: pathlib.Path, prefix_hash_filter: str | None = None):
    """Group completion rows by (essay_id, prefix_config_hash)."""
    groups: dict[tuple[str, str], dict] = {}
    prefix_text_by_group: dict[tuple[str, str], str] = {}
    for row in jsonl.read(log_path):
        k = (row["essay_id"], row["prefix_config_hash"])
        if prefix_hash_filter and row["prefix_config_hash"] != prefix_hash_filter:
            continue
        groups.setdefault(k, {})[row["source_id"]] = row["response_text"]
        if row["source_id"] != "human" and k not in prefix_text_by_group:
            pt = _prefix_text_from_completion_row(row)
            if pt:
                prefix_text_by_group[k] = pt
    return groups, prefix_text_by_group


def _call_one_judgment(
    essay_id,
    prefix_hash,
    a_id,
    b_id,
    first,
    second,
    criterion,
    judge_model,
    prompt,
    k,
    max_tokens,
    client,
):
    t0 = time.time()
    resp = openrouter.chat(
        model=judge_model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
        max_tokens=max_tokens,
        client=client,
    )
    text = openrouter.extract_text(resp)
    verdict = parse_verdict(text)
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
        "judge_model": judge_model,
        "verdict": verdict,
        "winner_source": winner_source,
        "reasoning_text": text,
        "prompt": prompt,
        "ts": dt.datetime.utcnow().isoformat() + "Z",
        "duration_s": round(time.time() - t0, 2),
        "raw_response": resp,
    }


def run(
    cfg: config.Config,
    *,
    judge_models: list[str] | None = None,
    criteria: list[str] | None = None,
    essay_ids: list[str] | None = None,
    contestants: list[str] | None = None,
    vs_human: bool = False,
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
    """
    groups, prefix_texts = load_sources_by_essay(cfg.storage.completions_log)
    existing = jsonl.keys(cfg.storage.judgments_log)

    effective_judges = judge_models if judge_models is not None else cfg.judging.models
    effective_criteria = criteria if criteria is not None else cfg.judging.criteria
    essay_id_set = set(essay_ids) if essay_ids else None
    contestants_set = set(contestants) if contestants else None

    tasks_to_run: list = []
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
            src_a = Source(a_id, sources[a_id])
            src_b = Source(b_id, sources[b_id])
            first, second = order_pair(essay_id, src_a, src_b)
            for criterion in effective_criteria:
                for judge_model in effective_judges:
                    k = judgment_key(essay_id, prefix_hash, a_id, b_id, criterion, judge_model)
                    if k in existing:
                        continue
                    prompt = render_judge_prompt(
                        prefix_text=prefix_text,
                        criterion=criterion,
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
                            judge_model,
                            prompt,
                            k,
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
            essay_id, _, a_id, b_id, _, _, crit, jm, _, _ = t
            print(f"  * {essay_id} {a_id} vs {b_id} [{crit}] -> {jm}")
        if len(tasks_to_run) > 20:
            print(f"  ... and {len(tasks_to_run) - 20} more")
        return
    client = httpx.Client(timeout=600.0)
    try:
        with ThreadPoolExecutor(max_workers=cfg.concurrency) as pool:
            futures = {
                pool.submit(_call_one_judgment, *t, cfg.judging.max_tokens, client): t[
                    9
                ]  # key is 10th element
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
