"""Fetch recent forethought.org essays per config, then validate each.

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

from versus import config, envcascade, fetch, validate_essay  # noqa: E402


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
    args = p.parse_args()

    cfg = config.load("config.yaml")
    raw_html_dir = cfg.essays.cache_dir.parent / "raw_html"
    essays = fetch.fetch(
        cache_dir=cfg.essays.cache_dir,
        raw_html_dir=raw_html_dir,
        max_recent=cfg.essays.max_recent,
    )
    print(f"fetched {len(essays)} essays:")
    for e in essays:
        types: dict[str, int] = {}
        for b in e.blocks:
            types[b.type] = types.get(b.type, 0) + 1
        print(f"  - {e.id}: {e.title!r} ({types})")

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
    for e in essays:
        verdict = validate_essay.validate(
            essay_id=e.id,
            markdown=e.markdown,
            cache_dir=cfg.essays.cache_dir,
            force=args.revalidate,
        )
        print(validate_essay.format_verdict(verdict))
        if not verdict["clean"]:
            failures.append(verdict)

    if failures:
        print(
            f"\n{len(failures)} essay(s) failed validation. "
            "Fix the parser in versus/src/versus/fetch.py and re-run, "
            "or pass --no-validate to import anyway."
        )
        sys.exit(1)
    print(f"\nall {len(essays)} essays clean.")


if __name__ == "__main__":
    main()
