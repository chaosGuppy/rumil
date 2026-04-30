"""Run the freeform view-essay prompt against a question.

Single LLM call, no tools, no workspace mutation. The point is to inspect what
the prompt produces before building a proper call type around it. The agent
loop / branching machinery comes later — this script exists so we can read a
few outputs first and decide whether the prompt is doing what we want.

Usage:
    uv run python scripts/run_view_essay.py --question-id <UUID>

    # Tag the output with a label (defaults to a timestamp slug)
    uv run python scripts/run_view_essay.py --question-id <UUID> --label v3-prompt

    # Print the rendered prompt before sending
    uv run python scripts/run_view_essay.py --question-id <UUID> --show-prompt

    # Use a different prompt file (e.g. the OneDrive working copy)
    uv run python scripts/run_view_essay.py --question-id <UUID> \\
        --prompt-file "C:/Users/scath/OneDrive/Documents/Rumil Tara prompt.md"
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path

from rumil.context import build_embedding_based_context
from rumil.database import DB
from rumil.llm import text_call
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


async def run(args: argparse.Namespace) -> None:
    settings = get_settings()

    if not args.question_id and not args.question_text:
        print("Provide --question-id or --question-text.")
        return

    run_id = str(uuid.uuid4())
    db = await DB.create(run_id=run_id, prod=False, staged=args.staged)
    project = await db.get_or_create_project(args.workspace)
    db.project_id = project.id

    if args.question_text:
        from rumil.orchestrators import create_root_question

        question_id = await create_root_question(args.question_text, db)
        print(
            f"Created question {question_id[:8]} in workspace '{args.workspace}'"
            f"{' (staged under run ' + run_id[:8] + ')' if args.staged else ''}"
        )
    else:
        question_id = args.question_id

    question = await db.get_page(question_id)
    if not question:
        print(f"Question {question_id} not found.")
        return
    if question.project_id and question.project_id != db.project_id:
        db.project_id = question.project_id

    print(f"Question: {question.headline}")
    if args.label:
        print(f"Label:    {args.label}")
    print(f"Model:    {settings.model}")
    print(f"Workspace: {args.workspace} (staged={args.staged}, run_id={run_id[:8]})")
    print()

    print("Building research context…")
    context_result = await build_embedding_based_context(
        question.headline,
        db,
        scope_question_id=question_id,
        require_take_for_questions=True,
    )
    print(f"Context:  {len(context_result.context_text)} chars")
    print()

    if args.prompt_file:
        # Override mode: a single file is treated as the entire user-message prompt
        # (legacy single-file Tara prompt). Substitute placeholders in-place.
        template = Path(args.prompt_file).read_text(encoding="utf-8")
        user_msg = render_template(
            template,
            {
                "QUESTION": question.headline,
                "RESEARCH_CONTEXT": context_result.context_text,
                "PRIOR_OUTPUTS": "",
            },
        )
        system_msg = ""
    else:
        # Default mode: system prompt = preamble (Tara methodology + workspace).
        # User message = context + per-call moves + task framing.
        preamble = (PROMPTS_DIR / "preamble.md").read_text(encoding="utf-8")
        moves = (PROMPTS_DIR / "view_essay.md").read_text(encoding="utf-8")
        embed_task = (
            f'the question being investigated: "{question.headline}"\n\n'
            "produce a freeform essay-style view on the question."
        )
        system_msg = preamble.replace("{{TASK}}", embed_task)
        user_msg = f"context for this question:\n\n{context_result.context_text}\n\n---\n\n{moves}"

    if args.show_prompt:
        print("=" * 80)
        print("SYSTEM PROMPT")
        print("=" * 80)
        print(system_msg)
        print("=" * 80)
        print("USER MESSAGE")
        print("=" * 80)
        print(user_msg)
        print("=" * 80)
        print()

    print(f"Calling {settings.model} (no tools, single shot)…")
    response = await text_call(system_prompt=system_msg, user_message=user_msg)

    if args.output:
        out_path = Path(args.output)
    else:
        out_dir = Path("tmp/view_essays")
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        if args.label:
            slug = re.sub(r"[^a-z0-9]+", "-", args.label.lower()).strip("-")
            out_path = out_dir / f"{ts}_{question_id[:8]}_{slug}.md"
        else:
            out_path = out_dir / f"{ts}_{question_id[:8]}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    label_line = f"- Label: {args.label}\n" if args.label else ""
    prompt_source = args.prompt_file if args.prompt_file else "preamble.md + view_essay.md (split)"
    out_path.write_text(
        "# View essay\n\n"
        f"- Question: {question.headline}\n"
        f"- Question ID: {question_id}\n"
        f"{label_line}"
        f"- Model: {settings.model}\n"
        f"- Workspace: {args.workspace} (staged={args.staged}, run_id={run_id})\n"
        f"- Context chars: {len(context_result.context_text)}\n"
        f"- Prompt source: {prompt_source}\n\n"
        "## Response\n\n"
        f"{response}\n",
        encoding="utf-8",
    )
    print(f"Saved to: {out_path}")
    print()
    print("=" * 80)
    print("RESPONSE")
    print("=" * 80)
    print(response)
    print("=" * 80)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the freeform view-essay prompt against a question.",
    )
    parser.add_argument(
        "--question-id",
        default=None,
        help="UUID of an existing question. Mutually exclusive with --question-text.",
    )
    parser.add_argument(
        "--question-text",
        default=None,
        help="Create a fresh question with this text (used when no --question-id given).",
    )
    parser.add_argument(
        "--staged",
        action="store_true",
        help=(
            "Run with staged=True so any pages created (e.g. via --question-text) "
            "and the run_id-tagged context isolation are invisible to other runs. "
            "Useful when testing against the default workspace without polluting it."
        ),
    )
    parser.add_argument(
        "--label",
        default=None,
        help=(
            "Free-form label tagged into the output filename slug "
            "(e.g. 'v3-prompt', 'longer-context'). Defaults to a timestamp."
        ),
    )
    parser.add_argument("--workspace", default="default")
    parser.add_argument(
        "--prompt-file",
        default=None,
        help=(
            "Override with a single self-contained prompt file (legacy mode); "
            "the file's contents are used as the user message with placeholder "
            "substitution. Default: load preamble.md + view_essay.md as system "
            "prompt and build a separate user message with the question + context."
        ),
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output path (default: tmp/view_essays/<ts>_<qid8>.md).",
    )
    parser.add_argument(
        "--show-prompt",
        action="store_true",
        help="Print the rendered prompt before sending.",
    )
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
