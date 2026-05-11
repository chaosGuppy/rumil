"""Resolve a workflow's variant YAML into ready-to-fire CLI invocations.

Reads ``.claude/skills/rumil-versus-iterate/variants/<workflow>.yaml``
and emits one CLI command per variant as a JSON list. The iterate
skill consumes the list and fires each command in parallel via
background ``Bash`` calls — the iterate skill body knows how to do
that, this helper just removes the hand-translation step.

Usage::

    PYTHONPATH=.claude/lib uv run python -m rumil_skills.fan_out \\
        --workflow reflective_judge \\
        --essay forethought__ai-for-decision-advice \\
        --workspace versus \\
        --dimension would_recommend \\
        --model sonnet \\
        --limit 1 \\
        [--contestants <source_a>,<source_b>] \\
        [--variant <name> ...]   # filter

Output (JSON list, one entry per matching variant)::

    [
      {"name": "baseline",
       "description": "All defaults — sonnet across all stages, ...",
       "cmd": ["uv", "run", "python", "versus/scripts/...", ...]},
      ...
    ]

Why JSON: the iterate skill iterates the list and dispatches each
``cmd`` array as a Bash background call. Plain text would force the
skill to parse shell-quoted strings; JSON arrays just pass through.

Two workflows currently registered:

- ``reflective_judge`` → ``versus/scripts/run_rumil_judgments.py``
  with ``--variant reflective``. Variant kwargs map directly to
  per-stage CLI flags.
- ``draft_and_edit`` → ``versus/scripts/run_completions.py``
  with ``--orch draft_and_edit``. Variant kwargs flow through the
  ``--workflow-arg key=value`` passthrough since the completion CLI
  doesn't expose per-stage flags directly.

Adding a third workflow: extend ``WORKFLOW_CLI`` below.

Pair-locking (``--contestants``): for ``reflective_judge``, every
variant invocation should fire against the same pair so verdicts are
directly comparable. The CLI's ``--contestants`` is threaded into
every variant's command. Without it, the planner picks the next
pending pair each time and variants drift to different pairs. The
YAML may also declare a ``default_contestants:`` top-level field as
a fallback when the CLI flag is omitted.

For ``draft_and_edit`` there is no pair concept — completions are
per (essay × prefix × workflow × model). ``--contestants`` is
ignored on that side.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SKILL_ROOT = _REPO_ROOT / ".claude" / "skills" / "rumil-versus-iterate"
_VARIANTS_DIR = _SKILL_ROOT / "variants"


WORKFLOW_CLI: dict[str, dict] = {
    "reflective_judge": {
        "script": "versus/scripts/run_rumil_judgments.py",
        "default_args": ["--variant", "reflective"],
        # variant key → CLI flag (each value emitted as `flag value`)
        "kwarg_flags": {
            "reader_model": "--reader-model",
            "reflector_model": "--reflector-model",
            "verdict_model": "--verdict-model",
            "read_prompt_path": "--read-prompt-path",
            "reflect_prompt_path": "--reflect-prompt-path",
            "verdict_prompt_path": "--verdict-prompt-path",
        },
        "supports_contestants": True,
    },
    "draft_and_edit": {
        "script": "versus/scripts/run_completions.py",
        "default_args": ["--orch", "draft_and_edit"],
        # d&e CLI doesn't expose per-stage flags; these flow through
        # --workflow-arg key=value instead.
        "kwarg_flags": {},
        "via_workflow_arg": [
            "n_critics",
            "max_rounds",
            "drafter_model",
            "critic_model",
            "editor_model",
            "drafter_prompt_path",
            "critic_prompt_path",
            "editor_prompt_path",
            "with_planner",
            "with_arbiter",
            "with_brief_audit",
            "planner_model",
            "arbiter_model",
            "audit_model",
            "planner_prompt_path",
            "arbiter_prompt_path",
            "audit_prompt_path",
            "brief_audit_after_round",
            "audit_feeds_critic",
            "with_scout_pass",
            "scout_pass_model",
            "scout_pass_prompt_path",
        ],
        "supports_contestants": False,
        # `budget` is a top-level CLI flag on this script; pull it
        # from variant config and pass as --budget directly.
        "budget_flag": "--budget",
    },
}


def _build_cmd(
    workflow: str,
    variant: dict,
    common_args: Sequence[str],
) -> list[str]:
    cfg = WORKFLOW_CLI[workflow]
    cmd: list[str] = ["uv", "run", "python", cfg["script"], *cfg["default_args"]]
    cmd.extend(common_args)

    # Per-key flag flags (reflective_judge).
    for key, flag in cfg.get("kwarg_flags", {}).items():
        if key in variant:
            cmd += [flag, str(variant[key])]

    # --workflow-arg passthrough (draft_and_edit).
    for key in cfg.get("via_workflow_arg", []):
        if key in variant:
            cmd += ["--workflow-arg", f"{key}={variant[key]}"]

    # --budget direct flag (draft_and_edit).
    if "budget_flag" in cfg and "budget" in variant:
        cmd += [cfg["budget_flag"], str(variant["budget"])]

    return cmd


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--workflow",
        required=True,
        choices=sorted(WORKFLOW_CLI.keys()),
        help="Which iteration-target workflow to fan out.",
    )
    ap.add_argument("--workspace", default="versus")
    ap.add_argument(
        "--essay",
        required=True,
        help="Essay id to scope every variant invocation to (single value).",
    )
    ap.add_argument(
        "--dimension",
        default=None,
        help="Judge dimension. Required-for-reflective; ignored for d&e.",
    )
    ap.add_argument(
        "--model",
        default=None,
        help="Bridge model id. Required for both workflows; "
        "reflective_judge accepts short aliases (sonnet/opus/haiku), "
        "draft_and_edit needs the full provider/model id.",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap on number of pairs/completions per variant. "
        "Reflective: defaults to no cap (planner picks all pending). "
        "D&E: not applicable.",
    )
    ap.add_argument(
        "--contestants",
        default=None,
        help="Lock all variants to a specific pair (reflective only). "
        "Comma-separated source_ids. Overrides any default_contestants "
        "declared in the variants YAML.",
    )
    ap.add_argument(
        "--vs-human",
        action="store_true",
        help="Reflective only — restrict to pairs where one side is the human continuation.",
    )
    ap.add_argument(
        "--prefix-label",
        default=None,
        help="Restrict to one prefix variant (id from cfg.prefix_variants).",
    )
    ap.add_argument(
        "--variant",
        action="append",
        default=None,
        help="Filter to specific variant(s) by name. Repeatable. "
        "If omitted, all variants in the YAML are emitted.",
    )
    ap.add_argument(
        "--prod",
        action="store_true",
        help="Threaded through as --prod on each variant's invocation.",
    )
    args = ap.parse_args()

    yaml_path = _VARIANTS_DIR / f"{args.workflow}.yaml"
    if not yaml_path.exists():
        print(f"error: no variants YAML at {yaml_path}", file=sys.stderr)
        sys.exit(1)
    spec = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    variants = spec.get("variants", []) if isinstance(spec, dict) else []
    if not variants:
        print(f"error: {yaml_path} has no `variants:` entries", file=sys.stderr)
        sys.exit(1)

    if args.variant:
        wanted = set(args.variant)
        variants = [v for v in variants if v.get("name") in wanted]
        if not variants:
            print(
                f"error: no variants in {yaml_path} match --variant {sorted(wanted)!r}",
                file=sys.stderr,
            )
            sys.exit(1)

    cfg = WORKFLOW_CLI[args.workflow]

    # Build the common CLI args once; reused across variants.
    common: list[str] = ["--workspace", args.workspace, "--essay", args.essay]
    if args.dimension:
        common += ["--dimension", args.dimension]
    if args.model:
        common += ["--model", args.model]
    if args.limit is not None:
        common += ["--limit", str(args.limit)]
    if args.vs_human:
        common += ["--vs-human"]
    if args.prefix_label:
        common += ["--prefix-label", args.prefix_label]
    if args.prod:
        common += ["--prod"]

    if cfg["supports_contestants"]:
        contestants = args.contestants or spec.get("default_contestants")
        if contestants:
            common += ["--contestants", contestants]

    out: list[dict] = []
    for v in variants:
        if not isinstance(v, dict) or "name" not in v:
            print(f"error: malformed variant entry in {yaml_path}: {v!r}", file=sys.stderr)
            sys.exit(1)
        out.append(
            {
                "name": v["name"],
                "description": (v.get("description") or "").strip(),
                "cmd": _build_cmd(args.workflow, v, common),
            }
        )

    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
