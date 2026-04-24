"""Fetch recent essays from every configured source, then validate each.

Validation runs Sonnet against the normalized markdown to flag scraping/
normalization artifacts. A failing essay (clean=False) blocks the import:
the script exits with status 1 and the user must fix the parser or pass
``--no-validate`` to opt out.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

VERSUS_ROOT = pathlib.Path(__file__).resolve().parent.parent
RUMIL_ROOT = VERSUS_ROOT.parent

sys.path.insert(0, str(VERSUS_ROOT / "src"))

from versus import config, envcascade, sources, validate_essay  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--no-validate",
        action="store_true",
        help="skip the Sonnet validator (default: validate every essay)",
    )
    p.add_argument(
        "--revalidate",
        action="store_true",
        help="ignore cached verdicts and re-run the validator",
    )
    p.add_argument(
        "--source",
        action="append",
        default=None,
        help="only fetch from these source ids (repeatable). Default: all configured.",
    )
    args = p.parse_args()

    cfg = config.load("config.yaml")
    raw_html_dir = cfg.essays.cache_dir.parent / "raw_html"

    source_cfgs = cfg.essays.sources
    if args.source:
        keep = set(args.source)
        source_cfgs = [s for s in source_cfgs if s.id in keep]
        if not source_cfgs:
            print(f"[err] no configured sources match --source {args.source}")
            sys.exit(1)

    essays = sources.fetch_all(
        source_cfgs=source_cfgs,
        cache_dir=cfg.essays.cache_dir,
        raw_html_dir=raw_html_dir,
    )

    thresholds = {s.id: s for s in source_cfgs}
    exclude = set(cfg.essays.exclude_ids)
    kept: list = []
    for e in essays:
        if e.id in exclude:
            print(f"  [skip-excluded] {e.id}")
            continue
        sc = thresholds.get(e.source_id)
        if sc is None:
            kept.append(e)
            continue
        ratio = e.image_ratio()
        over_count = sc.max_images is not None and e.image_count > sc.max_images
        over_ratio = sc.max_image_ratio is not None and ratio > sc.max_image_ratio
        if over_count or over_ratio:
            reason = []
            if over_count:
                reason.append(f"images={e.image_count}>max={sc.max_images}")
            if over_ratio:
                reason.append(f"ratio={ratio:.2f}>max={sc.max_image_ratio}")
            print(f"  [skip-image-heavy] {e.id}: {'; '.join(reason)}")
            continue
        kept.append(e)
    essays = kept

    print(f"fetched {len(essays)} essays:")
    for e in essays:
        types: dict[str, int] = {}
        for b in e.blocks:
            types[b.type] = types.get(b.type, 0) + 1
        tag = f"imgs={e.image_count}" if e.image_count else ""
        print(f"  - {e.id}: {e.title!r} ({types}) {tag}".rstrip())

    if args.no_validate:
        print("\n(validation skipped)")
        return

    envcascade.apply(
        ("ANTHROPIC_API_KEY",),
        versus_root=VERSUS_ROOT,
        rumil_root=RUMIL_ROOT,
    )

    print("\nvalidating essays:")
    failures: list[dict] = []
    errors: list[str] = []
    for e in essays:
        try:
            verdict = validate_essay.validate(
                essay_id=e.id,
                markdown=e.markdown,
                cache_dir=cfg.essays.cache_dir,
                force=args.revalidate,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  [err]  {e.id}: validator response unparseable — {exc}")
            errors.append(e.id)
            continue
        print(validate_essay.format_verdict(verdict))
        if not verdict["clean"]:
            failures.append(verdict)

    if errors:
        print(
            f"\n{len(errors)} essay(s) had unparseable validator responses. "
            "Re-run with --revalidate to retry, or pass --no-validate to skip."
        )
    if failures:
        print(
            f"\n{len(failures)} essay(s) failed validation. "
            "Fix the parser in versus/src/versus/sources/ and re-run, "
            "or pass --no-validate to import anyway."
        )
        sys.exit(1)
    if errors:
        sys.exit(1)
    print(f"\nall {len(essays)} essays clean.")


if __name__ == "__main__":
    main()
