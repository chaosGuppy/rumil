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

try:
    import rumil  # noqa: F401
except ModuleNotFoundError:
    sys.stderr.write(
        "[err] rumil isn't importable from this venv. Run from the rumil "
        "repo root, not versus/:\n"
        f"      cd {VERSUS_ROOT.parent} && uv run python versus/scripts/status.py ...\n"
    )
    raise SystemExit(1)

from versus import config, jsonl, judge, prepare  # noqa: E402
from versus import essay as versus_essay  # noqa: E402
from versus import mainline as versus_mainline  # noqa: E402
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
        if not versus_essay.is_current_schema(d):
            # Older schema — the API's staleness gate excludes these from
            # ``current_prefix_hashes`` so rows against them show as
            # "essay-not-current" in /versus. Match that here: drop the
            # essay, and its rows fall into ``unknown_essay`` below.
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
    cfg: config.Config,
    essays: dict[str, versus_essay.Essay],
    prefix_cfg: config.PrefixCfg | None = None,
) -> dict[str, str]:
    pcfg = prefix_cfg if prefix_cfg is not None else cfg.prefix
    out: dict[str, str] = {}
    for eid, essay in essays.items():
        task = prepare.prepare(
            essay,
            n_paragraphs=pcfg.n_paragraphs,
            include_headers=pcfg.include_headers,
            length_tolerance=cfg.completion.length_tolerance,
        )
        out[eid] = task.prefix_config_hash
    return out


def _current_paraphrase_sampling_hashes(cfg: config.Config) -> set[str]:
    return {_paraphrase.sampling_hash(m) for m in _paraphrase.paraphrase_models(cfg)}


def _scan_jsonl(path: pathlib.Path) -> list[dict]:
    if not path.is_file():
        return []
    return list(jsonl.read(path))


def _completions_per_variant(
    rows: list[dict],
    variant_hashes: dict[str, dict[str, str]],
) -> tuple[dict[str, int], int, int]:
    """Return (per_variant_current, orphaned, unknown_essay) counts.

    ``variant_hashes`` is ``{variant_id: {essay_id: prefix_hash}}``. A
    row counts toward variant V if its (essay_id, prefix_hash) matches
    V's current map. ``orphaned`` is rows whose essay is known to at
    least one variant but whose hash matches none — truly stale.
    """
    per_variant = dict.fromkeys(variant_hashes, 0)
    orphaned = 0
    unknown = 0
    for r in rows:
        eid = r.get("essay_id")
        ph = r.get("prefix_config_hash")
        if not any(eid in m for m in variant_hashes.values()):
            unknown += 1
            continue
        matched = False
        for vid, m in variant_hashes.items():
            if m.get(eid) == ph:
                per_variant[vid] += 1
                matched = True
                break
        if not matched:
            orphaned += 1
    return per_variant, orphaned, unknown


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


def _judgments_per_variant(
    rows: list[dict],
    variant_hashes: dict[str, dict[str, str]],
) -> tuple[dict[str, int], dict[str, int], int, int]:
    """Bucket judgments per variant.

    Returns ``(current_per_variant, prompt_stale_per_variant, orphaned, unknown)``.
    A row is "current under V" if its essay+hash match V's current map
    AND its judge prompt suffix is still current. If essay+hash match V
    but the prompt suffix is stale, it counts toward
    ``prompt_stale_per_variant[V]``. If the hash matches no variant's
    current map for its essay, it's ``orphaned`` (essay drift).
    """
    current = dict.fromkeys(variant_hashes, 0)
    prompt_stale = dict.fromkeys(variant_hashes, 0)
    orphaned = 0
    unknown = 0
    for r in rows:
        eid = r.get("essay_id")
        ph = r.get("prefix_config_hash")
        if not any(eid in m for m in variant_hashes.values()):
            unknown += 1
            continue
        matched_vid = None
        for vid, m in variant_hashes.items():
            if m.get(eid) == ph:
                matched_vid = vid
                break
        if matched_vid is None:
            orphaned += 1
            continue
        criterion = r.get("criterion")
        if r.get("judge_model") and criterion and not judge.judge_config_is_current(r, criterion):
            prompt_stale[matched_vid] += 1
        else:
            current[matched_vid] += 1
    return current, prompt_stale, orphaned, unknown


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--json", action="store_true", help="machine-readable output")
    args = p.parse_args()

    cfg = config.load(str(VERSUS_ROOT / "config.yaml"))
    if not cfg.essays.cache_dir.is_absolute():
        cfg.essays.cache_dir = VERSUS_ROOT / cfg.essays.cache_dir
    essays = _load_essays(cfg)
    if not essays:
        print("no cached essays — run scripts/fetch_essays.py first")
        sys.exit(1)
    variants = prepare.active_prefix_configs(cfg)
    variant_hashes = {v.id: _current_prefix_hashes(cfg, essays, prefix_cfg=v) for v in variants}
    current_samp = _current_paraphrase_sampling_hashes(cfg)

    completions = _scan_jsonl(VERSUS_ROOT / "data" / "completions.jsonl")
    paraphrases = _scan_jsonl(VERSUS_ROOT / "data" / "paraphrases.jsonl")
    judgments = _scan_jsonl(VERSUS_ROOT / "data" / "judgments.jsonl")

    comp_per_variant, comp_orphaned, comp_unknown = _completions_per_variant(
        completions, variant_hashes
    )
    para_counts = _stale_paraphrases(paraphrases, current_samp, set(essays))
    judg_current, judg_prompt_stale, judg_orphaned, judg_unknown = _judgments_per_variant(
        judgments, variant_hashes
    )

    payload = {
        "essays": len(essays),
        "variants": [v.id for v in variants],
        "completions": {
            "per_variant": comp_per_variant,
            "orphaned": comp_orphaned,
            "unknown_essay": comp_unknown,
            "total": len(completions),
        },
        "paraphrases": dict(para_counts),
        "judgments": {
            "per_variant_current": judg_current,
            "per_variant_prompt_stale": judg_prompt_stale,
            "orphaned": judg_orphaned,
            "unknown_essay": judg_unknown,
            "total": len(judgments),
        },
    }

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        any_stale = (
            comp_orphaned
            or para_counts["stale_prompt"]
            or judg_orphaned
            or sum(judg_prompt_stale.values())
        )
        if any_stale:
            print("=" * 60)
            print("WARNING: stale cached rows detected")
            print("=" * 60)
        print(f"essays cached: {len(essays)}")
        print(f"prefix variants: {', '.join(v.id for v in variants)}")
        for v in variants:
            cur = comp_per_variant[v.id]
            jcur = judg_current[v.id]
            jps = judg_prompt_stale[v.id]
            print(f"  variant {v.id!r}:")
            print(f"    completions current = {cur:5d}")
            ps_detail = f"  prompt_stale={jps}" if jps else ""
            print(f"    judgments   current = {jcur:5d}{ps_detail}")
        comp_total = len(completions)
        judg_total = len(judgments)
        print(
            f"  completions total={comp_total:5d}  "
            f"orphaned={comp_orphaned:5d}  unknown_essay={comp_unknown:3d}"
        )
        print(
            f"  paraphrases total={sum(para_counts.values()):5d}  "
            f"current={para_counts['current']:5d}  "
            f"stale_prompt={para_counts['stale_prompt']:5d}  "
            f"unknown_essay={para_counts['unknown_essay']:3d}"
        )
        print(
            f"  judgments   total={judg_total:5d}  "
            f"orphaned={judg_orphaned:5d}  unknown_essay={judg_unknown:3d}"
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
