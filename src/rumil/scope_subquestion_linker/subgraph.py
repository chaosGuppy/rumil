"""Render a depth-bounded subgraph of the question graph rooted at a question."""

from rumil.context import format_page
from rumil.database import DB
from rumil.models import PageDetail, PageType


async def render_question_subgraph(
    page_id: str,
    db: DB,
    *,
    max_depth: int = 3,
) -> str:
    """Render a subgraph of the question graph rooted at *page_id*.

    Walks `db.get_child_questions` up to *max_depth* hops out, rendering each
    node as a single headline line indented by depth. Cycle-safe.
    """
    resolved = await db.resolve_page_id(page_id)
    if resolved is None:
        return f'[Page "{page_id}" not found]'
    root = await db.get_page(resolved)
    if root is None:
        return f'[Page "{page_id}" not found]'
    if root.page_type != PageType.QUESTION:
        return (
            f"[Page `{resolved[:8]}` is not a question (type={root.page_type.value})]"
        )

    lines: list[str] = []
    await _render(root.id, db, max_depth, 0, lines, set())
    return "\n".join(lines)


async def _render(
    question_id: str,
    db: DB,
    max_depth: int,
    depth: int,
    lines: list[str],
    visited: set[str],
) -> None:
    if question_id in visited:
        prefix = "  " * depth
        lines.append(f"{prefix}[Q] `{question_id[:8]}` -- *** cycle detected ***")
        return
    visited = visited | {question_id}

    page = await db.get_page(question_id)
    if page is None:
        return

    prefix = "  " * depth
    lines.append(
        prefix + await format_page(page, PageDetail.HEADLINE, linked_detail=None, db=db)
    )

    children = await db.get_child_questions(question_id)
    if depth >= max_depth:
        if children:
            lines.append(
                f"{prefix}  ({len(children)} more sub-Q(s) not shown -- horizon)"
            )
        return

    for child in children:
        await _render(child.id, db, max_depth, depth + 1, lines, visited)
