"""Backfill structured ``config`` + ``config_hash`` on legacy judgment rows.

Reads ``versus/data/judgments.jsonl`` row-by-row; for each row that
lacks a ``config`` field, parse the flat ``judge_model`` string via
:func:`versus.mainline.parse_judge_components` and synthesize a
plausible config dict from those components + the row's ``sampling``
field. Compute ``config_hash`` over it; write both back onto the row.

No new LLM / API calls — pure file IO.

Unrecoverable rows (predate the ``:p<hash>:v<N>`` regime, i.e. bare
``google/gemini-3-flash-preview`` shapes) are left untouched and
counted in the summary so the big-picture cleanup can decide what to
do with them. Legacy ws/orch rows have no recorded
``code_fingerprint`` or ``workspace_contents_hash``; backfilled
configs use empty / sentinel values for those, which means a current
re-run will hash differently — correct, since "we don't know what
state the run was in" is its own slice.

Usage:

    uv run python versus/scripts/backfill_judge_config.py [--dry-run] [--path PATH]
"""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import sys
import tempfile
from collections import Counter

_HERE = pathlib.Path(__file__).resolve().parent
_VERSUS_SRC = _HERE.parent / "src"
if str(_VERSUS_SRC) not in sys.path:
    sys.path.insert(0, str(_VERSUS_SRC))

from versus.judge import infer_order, judgment_key  # noqa: E402
from versus.judge_config import compute_config_hash  # noqa: E402
from versus.mainline import parse_judge_components  # noqa: E402

_DEFAULT_PATH = _HERE.parent / "data" / "judgments.jsonl"


def _strip_prefix(value: str | None, prefix: str) -> str | None:
    if value is None or not value.startswith(prefix):
        return None
    return value[len(prefix) :]


def _try_synthesize_config(row: dict) -> dict | None:
    """Reconstruct a structured config from a legacy row's fields, or
    return None if the row's ``judge_model`` predates the version
    regime and can't be recovered.
    """
    jm = row.get("judge_model")
    if not jm:
        return None
    parts = parse_judge_components(jm)
    phash_raw = _strip_prefix(parts.get("judge_prompt_hash"), "p")
    bjv_raw = _strip_prefix(parts.get("judge_version"), "v")
    bjv: int
    shell_hash: str
    if phash_raw is None or bjv_raw is None:
        # Pre-versioning rows (e.g. bare ``google/gemini-3-flash-preview``).
        # Sentinel hash + version=0 clusters them together in the
        # provenance panel as "legacy unversioned" so they don't go
        # untracked.
        shell_hash = "legacy00"
        bjv = 0
    else:
        shell_hash = phash_raw
        try:
            bjv = int(bjv_raw)
        except ValueError:
            return None

    path = parts.get("judge_path", "")
    variant: str
    if path == "rumil:orch":
        variant = "orch"
    elif path == "rumil:ws":
        variant = "ws"
    else:
        # path == "rumil:text" / "blind" / missing — all legacy single-call
        # judge shapes that line up with the modern ``blind`` variant.
        variant = "blind"

    config: dict = {
        "variant": variant,
        "model": parts.get("judge_base_model", ""),
        "dimension": parts.get("judge_dimension", ""),
        "sampling": row.get("sampling"),
        "prompts": {
            "shell_hash": shell_hash,
            "blind_judge_version": bjv,
            # Legacy rows didn't record this; None keeps the
            # config-key shape stable across rows.
            "completion_prompt_version": None,
        },
    }
    if variant in ("ws", "orch"):
        config["workspace_id"] = parts.get("judge_workspace_id", "")
        config["tool_descriptions_hash"] = _strip_prefix(parts.get("judge_tool_hash"), "t") or ""
        config["pair_surface_hash"] = _strip_prefix(parts.get("judge_pair_hash"), "q") or ""
        # Not recoverable from legacy rows. Sentinels rather than
        # current-code values so re-running on today's code rightly
        # produces a different config_hash.
        config["code_fingerprint"] = {}
        config["workspace_contents_hash"] = ""
    if variant == "orch":
        budget_raw = _strip_prefix(parts.get("judge_budget"), "b")
        try:
            config["budget"] = int(budget_raw) if budget_raw is not None else None
        except ValueError:
            config["budget"] = None
        config["closer_hash"] = _strip_prefix(parts.get("judge_closer_hash"), "c") or ""
    return config


def _backfill(path: pathlib.Path, *, dry_run: bool) -> None:
    if not path.is_file():
        print(f"[err ] {path} not found")
        sys.exit(1)
    counts: Counter[str] = Counter()
    out_lines: list[str] = []
    with path.open() as f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                counts["invalid_json"] += 1
                out_lines.append(line)
                continue
            existing_cfg = row.get("config") if isinstance(row.get("config"), dict) else None
            if existing_cfg is None:
                cfg = _try_synthesize_config(row)
                if cfg is None:
                    counts["unrecoverable"] += 1
                    out_lines.append(line)
                    continue
                row["config"] = cfg
                counts["backfilled"] += 1
            else:
                cfg = existing_cfg
                counts["already_has_config"] += 1
            ch = compute_config_hash(cfg)
            row["config_hash"] = ch
            # Re-key onto config_hash regardless of whether config was
            # backfilled this pass. Older script versions wrote keys
            # ending in ``|<judge_model>|<order>``; today's
            # ``judgment_key`` uses ``|<config_hash>|<order>`` so we
            # need every row's key to reflect the current scheme.
            new_key = judgment_key(
                row.get("essay_id", ""),
                row.get("prefix_config_hash", ""),
                row.get("source_a", ""),
                row.get("source_b", ""),
                row.get("criterion", ""),
                ch,
                infer_order(row),
            )
            if row.get("key") != new_key:
                row["key"] = new_key
                counts["rekeyed"] += 1
            out_lines.append(json.dumps(row))

    print(
        f"[summary] backfilled={counts['backfilled']}  "
        f"rekeyed={counts['rekeyed']}  "
        f"already_has_config={counts['already_has_config']}  "
        f"unrecoverable={counts['unrecoverable']}  "
        f"invalid_json={counts['invalid_json']}"
    )

    if dry_run:
        print("[dry-run] not writing")
        return

    # Atomic replace via a sibling temp file so a crash mid-write
    # doesn't truncate the source.
    with tempfile.NamedTemporaryFile(
        mode="w", dir=str(path.parent), delete=False, suffix=".tmp"
    ) as tmp:
        for line in out_lines:
            tmp.write(line + "\n")
        tmp_path = pathlib.Path(tmp.name)
    backup_path = path.with_suffix(path.suffix + ".bak.backfill")
    shutil.copy2(path, backup_path)
    tmp_path.replace(path)
    print(f"[ok  ] wrote {path} (backup: {backup_path})")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--path", type=pathlib.Path, default=_DEFAULT_PATH)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    _backfill(args.path, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
