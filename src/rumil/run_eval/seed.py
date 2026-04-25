"""Seed context for run-eval agents.

The seed is deliberately compact: the scope question and its current
judgement(s) rendered at full content, plus a headline-only view of the
1-hop subgraph with overflow indicators for everything further out. The
agent is expected to drill into anything interesting via
``explore_subgraph`` / ``load_page`` — handing it every neighbor up-front
tends to produce wide-but-shallow reads rather than targeted digs.
"""

from rumil.context import format_page
from rumil.database import DB
from rumil.models import PageDetail
from rumil.workspace_exploration.explore import render_subgraph


async def build_eval_seed_context(
    question_id: str,
    db: DB,
    highlight_run_id: str | None = None,
) -> str:
    """Build the initial context for a run-eval agent."""
    resolved = await db.resolve_page_id(question_id)
    if resolved is None:
        return f'[Page "{question_id}" not found]'

    root = await db.get_page(resolved)
    if root is None:
        return f'[Page "{question_id}" not found]'

    parts: list[str] = ["## Scope question", ""]
    parts.append(
        await format_page(
            root,
            PageDetail.CONTENT,
            linked_detail=None,
            db=db,
            highlight_run_id=highlight_run_id,
        )
    )
    parts.append("")

    judgements = await db.get_judgements_for_question(resolved)
    judgement_ids: set[str] = set()
    if judgements:
        parts.append("## Current judgement")
        parts.append("")
        for judgement in judgements:
            judgement_ids.add(judgement.id)
            parts.append(
                await format_page(
                    judgement,
                    PageDetail.CONTENT,
                    linked_detail=None,
                    db=db,
                    highlight_run_id=highlight_run_id,
                )
            )
            parts.append("")

    parts.append("## Local subgraph (1-hop, headlines only)")
    parts.append("")
    subgraph_text = await render_subgraph(
        resolved,
        db,
        max_depth=1,
        exclude_ids=judgement_ids or None,
        highlight_run_id=highlight_run_id,
    )
    parts.append(subgraph_text)

    return "\n".join(parts).rstrip()
