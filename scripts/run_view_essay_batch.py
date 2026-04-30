"""Batch-run the freeform view-essay prompt N times in parallel.

Builds context once (one question, one workspace), then fires N runs with
bounded concurrency. Optionally splits into labelled groups (one
sub-directory per label).

Output layout:
- with labels:    tmp/view_essays/<batch_label>/<label-slug>/<n>.md
- without labels: tmp/view_essays/<batch_label>/<n>.md

Usage:
    # 10 runs of the same prompt
    uv run python scripts/run_view_essay_batch.py \\
        --question-text "When will we get autonomous robots..." \\
        --workspace view-essay-batch-1 \\
        --n-per-label 10 \\
        --concurrency 10

    # 5 runs each across multiple labelled prompt variants
    uv run python scripts/run_view_essay_batch.py \\
        --question-id <UUID> \\
        --workspace view-essay-batch-1 \\
        --labels "v1,v2,v3" \\
        --n-per-label 5
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

from rumil.context import build_embedding_based_context
from rumil.database import DB
from rumil.llm import text_call
from rumil.orchestrators import create_root_question
from rumil.prompts import PROMPTS_DIR
from rumil.settings import get_settings

log = logging.getLogger(__name__)


def render_template(template: str, vars: dict[str, str]) -> str:
    """Minimal handlebars-flavoured render: ``{{X}}`` substitution and
    ``{{#if X}}...{{/if}}`` conditionals (truthy when value is non-empty)."""

    def _cond(match: re.Match[str]) -> str:
        var = match.group(1).strip()
        body = match.group(2)
        return body if vars.get(var) else ""

    template = re.sub(
        r"\{\{#if\s+(\w+)\}\}(.*?)\{\{/if\}\}",
        _cond,
        template,
        flags=re.DOTALL,
    )
    for var, val in vars.items():
        template = template.replace("{{" + var + "}}", val)
    return template


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


async def run_one(
    *,
    label: str | None,
    n: int,
    rendered_system: str,
    rendered_user: str,
    question_text: str,
    question_id: str,
    workspace: str,
    model: str,
    out_dir: Path,
    sem: asyncio.Semaphore,
    progress_idx: int,
    progress_total: int,
    start_time: float,
) -> tuple[bool, Path, str]:
    """Run a single LLM call. Returns (success, path, message)."""
    out_path = (out_dir / slugify(label) / f"{n}.md") if label else (out_dir / f"{n}.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tag = f"{label} #{n}" if label else f"#{n}"
    async with sem:
        local_start = time.monotonic()
        try:
            response = await text_call(
                system_prompt=rendered_system,
                user_message=rendered_user,
            )
        except Exception as e:
            elapsed_local = time.monotonic() - local_start
            elapsed_total = time.monotonic() - start_time
            return (
                False,
                out_path,
                (
                    f"[{progress_idx}/{progress_total}] {tag} FAILED in "
                    f"{elapsed_local:.0f}s (total {elapsed_total:.0f}s): "
                    f"{type(e).__name__}: {e}"
                ),
            )
        elapsed_local = time.monotonic() - local_start
    label_line = f"- Label: {label}\n" if label else ""
    out_path.write_text(
        "# View essay (batch)\n\n"
        f"- Question: {question_text}\n"
        f"- Question ID: {question_id}\n"
        f"{label_line}"
        f"- Run number: {n}\n"
        f"- Workspace: {workspace}\n"
        f"- Model: {model}\n\n"
        "## Response\n\n"
        f"{response}\n",
        encoding="utf-8",
    )
    elapsed_total = time.monotonic() - start_time
    rel = out_path.relative_to(out_path.parent.parent) if label else out_path.name
    return (
        True,
        out_path,
        (
            f"[{progress_idx}/{progress_total}] {tag} → {rel} "
            f"({elapsed_local:.0f}s, total {elapsed_total:.0f}s)"
        ),
    )


async def run(args: argparse.Namespace) -> None:
    settings = get_settings()

    labels: list[str | None]
    if args.labels_file:
        labels = [
            line.strip()
            for line in Path(args.labels_file).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]
        if not labels:
            print("No labels parsed from file.")
            return
    elif args.labels:
        labels = [a.strip() for a in args.labels.split(",") if a.strip()]
        if not labels:
            print("No labels parsed.")
            return
    else:
        labels = [None]

    if not args.question_id and not args.question_text:
        print("Provide --question-id or --question-text.")
        return

    if labels == [None]:
        print(f"Labels:       (none — flat output)")
    else:
        print(f"Labels:       {len(labels)}")
    print(f"N per label:  {args.n_per_label}")
    print(f"Total runs:   {len(labels) * args.n_per_label}")
    print(f"Concurrency:  {args.concurrency}")
    print(f"Model:        {settings.model}")
    print()

    run_id = str(uuid.uuid4())
    db = await DB.create(run_id=run_id, prod=False, staged=False)
    project = await db.get_or_create_project(args.workspace)
    db.project_id = project.id

    if args.question_text and not args.question_id:
        question_id = await create_root_question(args.question_text, db)
        print(f"Created question {question_id[:8]} in workspace '{args.workspace}'")
    else:
        question_id = args.question_id

    question = await db.get_page(question_id)
    if not question:
        print(f"Question {question_id} not found.")
        return

    print(f"Question:  {question.headline}")
    print(f"Workspace: {args.workspace}")
    print()
    print("Building research context (once for all runs)…")
    context_result = await build_embedding_based_context(
        question.headline,
        db,
        scope_question_id=question_id,
        require_take_for_questions=True,
    )
    print(f"Context: {len(context_result.context_text)} chars")
    print()

    if args.prompt_file:
        # Override mode: a single file is treated as the entire user-message
        # prompt (legacy single-file Tara). Substitute placeholders in-place.
        template = Path(args.prompt_file).read_text(encoding="utf-8")
        rendered_user = render_template(
            template,
            {
                "QUESTION": question.headline,
                "RESEARCH_CONTEXT": context_result.context_text,
                "PRIOR_OUTPUTS": "",
            },
        )
        rendered_system = ""
        prompt_source = str(Path(args.prompt_file))
    else:
        # Default mode: system prompt = preamble (Tara methodology + workspace).
        # User message = context + per-call moves.
        preamble = (PROMPTS_DIR / "preamble.md").read_text(encoding="utf-8")
        moves = (PROMPTS_DIR / "view_essay.md").read_text(encoding="utf-8")
        embed_task = (
            f'the question being investigated: "{question.headline}"\n\n'
            "produce a freeform essay-style view on the question."
        )
        rendered_system = preamble.replace("{{TASK}}", embed_task)
        rendered_user = (
            "context for this question:\n\n"
            f"{context_result.context_text}\n\n"
            "---\n\n"
            f"{moves}"
        )
        prompt_source = "preamble.md (system) + view_essay.md (user)"

    batch_label = args.batch_label or datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = Path("tmp/view_essays") / batch_label
    out_dir.mkdir(parents=True, exist_ok=True)

    labels_str = ", ".join(str(label) for label in labels) if labels != [None] else "(none)"
    manifest = (
        "# Batch manifest\n\n"
        f"- Started: {datetime.now().isoformat(timespec='seconds')}\n"
        f"- Question: {question.headline}\n"
        f"- Question ID: {question_id}\n"
        f"- Workspace: {args.workspace}\n"
        f"- Model: {settings.model}\n"
        f"- Prompt source: {prompt_source}\n"
        f"- Context chars: {len(context_result.context_text)}\n"
        f"- Labels ({len(labels)}): {labels_str}\n"
        f"- N per label: {args.n_per_label}\n"
        f"- Concurrency: {args.concurrency}\n"
    )
    (out_dir / "MANIFEST.md").write_text(manifest, encoding="utf-8")
    print(f"Output dir: {out_dir}")
    print()

    sem = asyncio.Semaphore(args.concurrency)
    start_time = time.monotonic()

    progress_total = len(labels) * args.n_per_label
    tasks = []
    idx = 0
    for label in labels:
        for n in range(1, args.n_per_label + 1):
            idx += 1
            tasks.append(
                run_one(
                    label=label,
                    n=n,
                    rendered_system=rendered_system,
                    rendered_user=rendered_user,
                    question_text=question.headline,
                    question_id=question_id,
                    workspace=args.workspace,
                    model=settings.model,
                    out_dir=out_dir,
                    sem=sem,
                    progress_idx=idx,
                    progress_total=progress_total,
                    start_time=start_time,
                )
            )

    successes = 0
    failures: list[str] = []
    for coro in asyncio.as_completed(tasks):
        success, _path, msg = await coro
        print(msg, flush=True)
        if success:
            successes += 1
        else:
            failures.append(msg)

    elapsed = time.monotonic() - start_time
    print()
    print(
        f"Done in {elapsed / 60:.1f}m. {successes}/{progress_total} succeeded, "
        f"{len(failures)} failed."
    )
    if failures:
        print()
        print("Failures:")
        for f in failures:
            print(f"  {f}")
    print(f"Outputs in: {out_dir}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Batch-run the view-essay prompt N times in parallel.",
    )
    parser.add_argument("--question-id", default=None)
    parser.add_argument("--question-text", default=None)
    parser.add_argument(
        "--labels",
        default=None,
        help=(
            "Optional comma-separated labels for splitting runs into "
            "subdirectories (e.g. 'v1,v2'). Default: all runs share one flat "
            "directory."
        ),
    )
    parser.add_argument(
        "--labels-file",
        default=None,
        help="One label per line; lines starting with # are ignored.",
    )
    parser.add_argument("--n-per-label", type=int, default=5)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--workspace", required=True)
    parser.add_argument(
        "--batch-label",
        default=None,
        help="Subdirectory under tmp/view_essays/ (default: timestamp)",
    )
    parser.add_argument("--prompt-file", default=None)
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
