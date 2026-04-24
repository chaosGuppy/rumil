"""Report staleness of cached completions / paraphrases / judgments.

After an essay re-import (any change that bumps ``prefix_config_hash``)
the existing rows in ``data/*.jsonl`` reference the OLD essay markdown.
They aren't deleted — they just no longer reflect what ``prepare()``
would produce today. Topup runs against stale rows judge old text.

Usage:

    uv run python scripts/status.py              # human-readable summary
    uv run python scripts/status.py --json       # machine-readable
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from collections import Counter

VERSUS_ROOT = pathlib.Path(__file__).resolve().parent.parent

sys.path.insert(0, str(VERSUS_ROOT / "src"))

from versus import config, jsonl, judge, prepare  # noqa: E402
from versus import essay as versus_essay  # noqa: E402
from versus import paraphrase as _paraphrase  # noqa: E402


def _load_essays(cfg: config.Config) -> dict[str, versus_essay.Essay]:
    exclude = set(cfg.essays.exclude_ids)
    out: dict[str, versus_essay.Essay] = {}
    for path in sorted(cfg.essays.cache_dir.glob("*.json")):
        if path.name.endswith(".verdict.json"):
            continue
        d = json.loads(path.read_text())
        if "source_id" not in d:
            # Legacy pre-multi-source JSON — skip. Re-fetch to upgrade.
            continue
        if d["id"] in exclude:
            continue
        out[d["id"]] = versus_essay.Essay(
            id=d["id"],
            source_id=d["source_id"],
            url=d["url"],
            title=d["title"],
            author=d["author"],
            pub_date=d["pub_date"],
            blocks=[versus_essay.Block(**b) for b in d["blocks"]],
            markdown=d["markdown"],
            image_count=d.get("image_count", 0),
            schema_version=d["schema_version"],
        )
    return out


def _current_prefix_hashes(
    cfg: config.Config, essays: dict[str, versus_essay.Essay]
) -> dict[str, str]:
    out: dict[str, str] = {}
    for eid, essay in essays.items():
        task = prepare.prepare(
            essay,
            n_paragraphs=cfg.prefix.n_paragraphs,
            include_headers=cfg.prefix.include_headers,
            length_tolerance=cfg.completion.length_tolerance,
        )
        out[eid] = task.prefix_config_hash
    return out


def _current_paraphrase_sampling_hashes(cfg: config.Config) -> set[str]:
    return {_paraphrase.sampling_hash(m) for m in cfg.paraphrasing.models}


def _scan_jsonl(path: pathlib.Path) -> list[dict]:
    if not path.is_file():
        return []
    return list(jsonl.read(path))


def _stale_completions(rows: list[dict], current_prefix: dict[str, str]) -> Counter:
    c = Counter({"current": 0, "stale_prefix": 0, "unknown_essay": 0})
    for r in rows:
        eid = r.get("essay_id")
        ph = r.get("prefix_config_hash")
        if eid not in current_prefix:
            c["unknown_essay"] += 1
        elif ph != current_prefix[eid]:
            c["stale_prefix"] += 1
        else:
            c["current"] += 1
    return c


def _stale_paraphrases(rows: list[dict], current_samp: set[str], known_essays: set[str]) -> Counter:
    c = Counter({"current": 0, "stale_prompt": 0, "unknown_essay": 0})
    for r in rows:
        eid = r.get("essay_id")
        sh = r.get("sampling_hash")
        if eid not in known_essays:
            c["unknown_essay"] += 1
        elif sh not in current_samp:
            c["stale_prompt"] += 1
        else:
            c["current"] += 1
    return c


def _stale_judgments(rows: list[dict], current_prefix: dict[str, str]) -> Counter:
    """Bucket judgment rows into current / stale_prefix / stale_prompt / unknown.

    A row is stale_prefix if its essay changed (``prefix_config_hash``
    drift — same thing flagged for completions).

    A row is stale_prompt if its ``judge_model`` suffix no longer matches
    the current ``p<hash>:v<N>``. That fires when versus-judge-shell.md
    or a versus-<dim>.md edit forks the prompt hash, or when
    ``JUDGE_PROMPT_VERSION`` / ``BLIND_JUDGE_VERSION`` is bumped. Without
    this bucket, prompt edits silently orphan existing rows and the
    status banner reads "all current" right up until the judging-quality
    regression shows up in /results.
    """
    c = Counter({"current": 0, "stale_prefix": 0, "stale_prompt": 0, "unknown_essay": 0})
    for r in rows:
        eid = r.get("essay_id")
        ph = r.get("prefix_config_hash")
        if eid not in current_prefix:
            c["unknown_essay"] += 1
            continue
        if ph != current_prefix[eid]:
            c["stale_prefix"] += 1
            continue
        judge_model = r.get("judge_model")
        criterion = r.get("criterion")
        if judge_model and criterion and not judge.judge_prompt_is_current(judge_model, criterion):
            c["stale_prompt"] += 1
            continue
        c["current"] += 1
    return c


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--json", action="store_true", help="machine-readable output")
    args = p.parse_args()

    cfg = config.load(str(VERSUS_ROOT / "config.yaml"))
    essays = _load_essays(cfg)
    if not essays:
        print("no cached essays — run scripts/fetch_essays.py first")
        sys.exit(1)
    current_prefix = _current_prefix_hashes(cfg, essays)
    current_samp = _current_paraphrase_sampling_hashes(cfg)

    completions = _scan_jsonl(VERSUS_ROOT / "data" / "completions.jsonl")
    paraphrases = _scan_jsonl(VERSUS_ROOT / "data" / "paraphrases.jsonl")
    judgments = _scan_jsonl(VERSUS_ROOT / "data" / "judgments.jsonl")

    comp_counts = _stale_completions(completions, current_prefix)
    para_counts = _stale_paraphrases(paraphrases, current_samp, set(essays))
    judg_counts = _stale_judgments(judgments, current_prefix)

    payload = {
        "essays": len(essays),
        "completions": dict(comp_counts),
        "paraphrases": dict(para_counts),
        "judgments": dict(judg_counts),
    }

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        any_stale = (
            comp_counts["stale_prefix"]
            or para_counts["stale_prompt"]
            or judg_counts["stale_prefix"]
            or judg_counts["stale_prompt"]
        )
        if any_stale:
            print("=" * 60)
            print("WARNING: stale cached rows detected")
            print("=" * 60)
        print(f"essays cached: {len(essays)}")
        for label, counts, stale_keys in (
            ("completions", comp_counts, ("stale_prefix",)),
            ("paraphrases", para_counts, ("stale_prompt",)),
            ("judgments", judg_counts, ("stale_prefix", "stale_prompt")),
        ):
            total = sum(counts.values())
            cur = counts["current"]
            stale = sum(counts[k] for k in stale_keys)
            unk = counts["unknown_essay"]
            mark = " <- STALE" if stale else ""
            detail = ""
            if len(stale_keys) > 1 and stale:
                detail = "  (" + ", ".join(f"{k}={counts[k]}" for k in stale_keys) + ")"
            print(
                f"  {label:13s} total={total:5d}  current={cur:5d}  "
                f"stale={stale:5d}  unknown_essay={unk:3d}{mark}{detail}"
            )
        if any_stale:
            print()
            print(
                "Topup runs against stale rows will judge old essay text or "
                "use an outdated judge prompt. To regenerate against current "
                "essays + prompts, run:"
            )
            print("  uv run python scripts/run_paraphrases.py")
            print("  uv run python scripts/run_completions.py")
            print("  uv run python scripts/run_judgments.py")
            print("  uv run python scripts/run_rumil_judgments.py  # rumil-style judges")
            sys.exit(2)
        print("\nall cached rows match current essays + prompts.")


if __name__ == "__main__":
    main()
