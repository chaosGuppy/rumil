"""Re-fire just the orch closer against an existing question page.

Cheap way to test closer-prompt / closer-rendering / closer-tool
changes without paying for another full TwoPhaseOrchestrator cycle.
Reuses the orchestrator's existing staged subtree (considerations,
judgements, views, view_items) — they're already persisted against
the original staged run_id.

Usage:

    uv run python versus/scripts/rerun_orch_closer.py \\
        --run-id <orch-run-id> \\
        --question-id <question-page-id> \\
        --model claude-sonnet-4-6 \\
        --render-variant default|expanded|view-only

Writes a new VERSUS_JUDGE call into the same run so the trace UI
shows it alongside the original closer. Does NOT write a judgment
row to ``judgments.jsonl`` — this is just for inspecting the new
closer's output.
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys

VERSUS_ROOT = pathlib.Path(__file__).resolve().parent.parent
RUMIL_ROOT = VERSUS_ROOT.parent

sys.path.insert(0, str(VERSUS_ROOT / "src"))
sys.path.insert(0, str(RUMIL_ROOT / "src"))

from rumil.context import format_page, render_view  # noqa: E402
from rumil.database import DB  # noqa: E402
from rumil.models import PageDetail  # noqa: E402
from rumil.settings import override_settings  # noqa: E402
from rumil.versus_bridge import (  # noqa: E402
    _render_question_for_closer,
    _run_orch_closer,
    extract_preference,
    label_to_verdict,
)
from rumil.versus_prompts import get_rumil_dimension_body  # noqa: E402


async def _render_expanded(db: DB, question_id: str) -> str:
    """Closer render augmented with child-question subtrees + source excerpts.

    :func:`_render_question_for_closer` on the scope question only surfaces
    (a) its own considerations at CONTENT detail and (b) child questions
    with judgements attached. In practice orchestrators drop considerations
    on sub-questions without necessarily writing judgements, so child
    research becomes invisible to the closer. Similarly, web_research
    creates Source pages that may or may not be promoted to top-level
    considerations. This render makes both visible.
    """
    base = await _render_question_for_closer(db, question_id)

    # Child-question subtrees: each child's own considerations, rendered at
    # ABSTRACT detail (keep prompt size manageable; full CONTENT on all
    # sub-question claims would balloon past 200k).
    children = await db.get_child_questions(question_id)
    child_blocks: list[str] = []
    for child in children:
        cons = await db.get_considerations_for_question(child.id)
        if not cons:
            continue
        lines = [f"### Sub-question `{child.id[:8]}` — {child.headline}"]
        for claim, link in cons:
            rendered = await format_page(claim, PageDetail.ABSTRACT, db=db, linked_detail=None)
            line = f"- {rendered}"
            if link.reasoning:
                line += f"\n  _Why linked: {link.reasoning}_"
            lines.append(line)
        child_blocks.append("\n".join(lines))
    child_section = (
        "\n\n## Sub-question research\n\n"
        "_Considerations the orchestrator linked to sub-questions nested under "
        "the scope. Not rendered by default because none carry judgements._\n\n"
        + "\n\n".join(child_blocks)
        if child_blocks
        else ""
    )

    # Source pages from this run. Sources are created by web_research and
    # stored as pages with type='source'; they may or may not be linked as
    # considerations. List them with headline + a short content excerpt so
    # the closer can see what external material the orch gathered.
    sr = (
        await db.client.table("pages")
        .select("id, headline, content, extra")
        .eq("run_id", db.run_id)
        .eq("page_type", "source")
        .execute()
    )
    source_block = ""
    if sr.data:
        lines = []
        for s in sr.data:
            url = (s.get("extra") or {}).get("url", "")
            url_tag = f" ({url})" if url else ""
            content = (s.get("content") or "").strip().splitlines()
            excerpt = " ".join(content[:3])[:400]
            lines.append(f"- `{s['id'][:8]}` — {s['headline']}{url_tag}\n  {excerpt}")
        source_block = (
            "\n\n## Sources gathered during this run\n\n"
            "_Source pages created by web_research calls, with brief excerpts. "
            "Use `load_page` for full content if needed._\n\n" + "\n".join(lines)
        )

    return base + child_section + source_block


async def _render_view_only(db: DB, question_id: str) -> str:
    """Closer render with top-level considerations stripped; view + view_items kept.

    Probes whether the consideration layer (individual claims linked to the
    scope question, rendered at CONTENT) adds any signal beyond the
    orchestrator's distilled View + view_items. If verdicts match the
    default render exactly, the consideration-level CONTENT was redundant
    and we can shrink prompts materially without losing anything. If
    verdicts diverge, claim bodies are load-bearing and the view is not a
    sufficient summary.
    """
    question = await db.get_page(question_id)
    if question is None:
        raise RuntimeError(f"question {question_id} missing after orch run")
    body = await format_page(question, PageDetail.CONTENT, linked_detail=None, db=db)
    view = await db.get_view_for_question(question_id)
    if view is None:
        return body
    items = await db.get_view_items(view.id, min_importance=2)
    view_rendered = await render_view(view, items, min_importance=2)
    return f"{body}\n\n{view_rendered}"


_RENDER_VARIANTS = {
    "default": None,  # uses _run_orch_closer's built-in default
    "expanded": _render_expanded,
    "view-only": _render_view_only,
}


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-id", required=True, help="Original orch run id.")
    ap.add_argument("--question-id", required=True, help="Versus question page id.")
    ap.add_argument(
        "--model",
        required=True,
        help="Anthropic model id (e.g. claude-sonnet-4-6). Same knob as run_rumil_judgments.py --model.",
    )
    ap.add_argument(
        "--task-name",
        default="general_quality",
        help="Dimension name for the task body (default: general_quality).",
    )
    ap.add_argument(
        "--render-variant",
        default="default",
        choices=sorted(_RENDER_VARIANTS.keys()),
        help=(
            "Which closer render to use. 'default' is _render_question_for_closer "
            "(production behavior). 'expanded' adds child-question subtrees + "
            "source excerpts. 'view-only' strips top-level considerations at "
            "CONTENT, keeps the View + view_items."
        ),
    )
    args = ap.parse_args()

    task_body = get_rumil_dimension_body(args.task_name)
    render_fn = _RENDER_VARIANTS[args.render_variant]

    # Resolve the original run's project_id so the new closer call can be
    # saved (save_call rejects empty UUIDs). Reusing the existing run_id +
    # staged view means the new call lands alongside the original orch's
    # persisted subtree without any data copying.
    bootstrap = await DB.create(run_id="_rerun_closer_bootstrap", prod=False, staged=False)
    run_row = await bootstrap.get_run(args.run_id)
    if not run_row:
        print(f"[err] run {args.run_id} not found", file=sys.stderr)
        sys.exit(1)
    project_id = run_row["project_id"]
    db = await DB.create(run_id=args.run_id, prod=False, project_id=project_id, staged=True)

    with override_settings(rumil_model_override=args.model):
        report_text, call, _system_prompt, _user_prompt = await _run_orch_closer(
            db,
            args.question_id,
            task_body=task_body,
            broadcaster=None,
            render_fn=render_fn,
        )

    label = extract_preference(report_text)
    verdict = label_to_verdict(label)
    print(f"[closer-call-id] {call.id}")
    print(f"[render-variant] {args.render_variant}")
    print(f"[label] {label}")
    print(f"[verdict] {verdict}")
    print(f"[cost-usd] {call.cost_usd}")
    print()
    print("=== closer report ===")
    print(report_text)


if __name__ == "__main__":
    asyncio.run(main())
