"""Report staleness of cached completions / judgments in versus_*.

After an essay re-import (any change that bumps ``prefix_config_hash``)
the existing rows in versus_texts / versus_judgments reference the OLD
essay markdown. They aren't deleted — they just no longer reflect what
``prepare()`` would produce today. Topup runs against stale rows judge
old text.

Paraphrase tracking is deferred (paraphrase generation is not currently
wired in this branch); when it comes back add a third section here.

Usage:

    uv run python scripts/status.py              # human-readable summary
    uv run python scripts/status.py --json       # machine-readable
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

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
    raise SystemExit(1) from None

from versus import config, judge, prepare, versus_db  # noqa: E402
from versus import essay as versus_essay  # noqa: E402


def _load_essays(cfg: config.Config, *, prod: bool = False) -> dict[str, versus_essay.Essay]:
    exclude = set(cfg.essays.exclude_ids)
    client = versus_db.get_client(prod=prod)
    return {e.id: e for e in prepare.load_essays(client) if e.id not in exclude}


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


def _scan_texts(*, prod: bool = False) -> list[dict]:
    client = versus_db.get_client(prod=prod)
    return list(versus_db.iter_texts(client))


def _scan_judgments(*, prod: bool = False) -> list[dict]:
    client = versus_db.get_client(prod=prod)
    return list(versus_db.iter_judgments(client))


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
        ph = r.get("prefix_hash")
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
        ph = r.get("prefix_hash")
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
    p.add_argument(
        "--prod",
        action="store_true",
        help="Inspect the production Supabase database (default: local).",
    )
    args = p.parse_args()

    cfg = config.load(str(VERSUS_ROOT / "config.yaml"))
    if not cfg.essays.cache_dir.is_absolute():
        cfg.essays.cache_dir = VERSUS_ROOT / cfg.essays.cache_dir
    essays = _load_essays(cfg, prod=args.prod)
    if not essays:
        target = "prod" if args.prod else "local"
        print(f"no essays in {target} versus_essays — run backfill_db.py / fetch_essays.py first")
        sys.exit(1)
    variants = prepare.active_prefix_configs(cfg)
    variant_hashes = {v.id: _current_prefix_hashes(cfg, essays, prefix_cfg=v) for v in variants}

    completions = _scan_texts(prod=args.prod)
    judgments = _scan_judgments(prod=args.prod)

    comp_per_variant, comp_orphaned, comp_unknown = _completions_per_variant(
        completions, variant_hashes
    )
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
        any_stale = comp_orphaned or judg_orphaned or sum(judg_prompt_stale.values())
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
            print("  uv run python scripts/run_completions.py")
            print("  uv run python scripts/run_rumil_judgments.py  # blind / ws / orch judges")
            sys.exit(2)
        print("\nall cached rows match current essays + prompts.")


if __name__ == "__main__":
    main()
