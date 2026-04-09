"""Render a depth-bounded subgraph of the question graph rooted at a question."""

from rumil.context import format_page
from rumil.database import DB
from rumil.models import LinkType, Page, PageDetail, PageType


async def render_question_subgraph(
    page_id: str,
    db: DB,
    *,
    max_depth: int = 6,
    max_pages: int | None = None,
    exclude_ids: set[str] | None = None,
) -> str:
    """Render a subgraph of the question graph rooted at *page_id*.

    Walks child-question links level-by-level, rendering each node as a single
    headline line indented by depth. Stops expanding when *either* `max_depth`
    is reached *or* the cumulative number of loaded question pages reaches
    `max_pages` (if set). Parents whose children weren't expanded are tagged
    with an overflow count. Cycle-safe.

    Any page id in *exclude_ids* is pruned: it is never rendered, its children
    are never fetched, and it is not counted toward `max_pages`. If the root
    itself is excluded, returns an empty string.

    Fetches each level in two batched queries (links + pages), so the total
    number of round trips is O(depth-actually-walked) regardless of fan-out.
    """
    excluded = exclude_ids or set()
    resolved = await db.resolve_page_id(page_id)
    if resolved is None:
        return f'[Page "{page_id}" not found]'
    if resolved in excluded:
        return ""
    root = await db.get_page(resolved)
    if root is None:
        return f'[Page "{page_id}" not found]'
    if root.page_type != PageType.QUESTION:
        return (
            f"[Page `{resolved[:8]}` is not a question (type={root.page_type.value})]"
        )

    pages_by_id: dict[str, Page] = {root.id: root}
    children_by_parent: dict[str, list[str]] = {}
    overflow_by_parent: dict[str, int] = {}
    visited: set[str] = {root.id}

    frontier: list[str] = [root.id]
    for depth in range(max_depth + 1):
        if not frontier:
            break
        links_by_parent = await db.get_links_from_many(frontier)
        parent_child_links: dict[str, list[str]] = {}
        next_ids: set[str] = set()
        for parent_id in frontier:
            child_links = [
                l for l in links_by_parent.get(parent_id, [])
                if l.link_type == LinkType.CHILD_QUESTION
                and l.to_page_id not in excluded
            ]
            child_ids = [l.to_page_id for l in child_links]
            parent_child_links[parent_id] = child_ids
            for cid in child_ids:
                if cid not in visited:
                    next_ids.add(cid)

        if depth == max_depth:
            # Hit the depth horizon: record any children as overflow so the
            # parent shows a "(N more sub-Q(s) not shown -- horizon)" note.
            for parent_id, child_ids in parent_child_links.items():
                if child_ids:
                    overflow_by_parent[parent_id] = len(child_ids)
            break

        if not next_ids:
            # No new pages to load this level: every child is already
            # visited (diamond/back-edge in the DAG). Render them as
            # leaves under their parents -- they're not horizon overflow
            # because they appear elsewhere in the tree.
            for parent_id, child_ids in parent_child_links.items():
                children_by_parent[parent_id] = child_ids
            break

        fetched = await db.get_pages_by_ids(list(next_ids))
        new_pages = {
            cid: fetched[cid]
            for cid in next_ids
            if fetched.get(cid) is not None
            and fetched[cid].page_type == PageType.QUESTION
        }

        if max_pages is not None and len(pages_by_id) + len(new_pages) > max_pages:
            for parent_id, child_ids in parent_child_links.items():
                if child_ids:
                    overflow_by_parent[parent_id] = len(child_ids)
            break

        for parent_id, child_ids in parent_child_links.items():
            children_by_parent[parent_id] = child_ids
        for cid, page in new_pages.items():
            pages_by_id[cid] = page
            visited.add(cid)
        frontier = [cid for cid in next_ids if cid in pages_by_id]

    judgements_by_question = await db.get_judgements_for_questions(
        list(pages_by_id.keys())
    )
    robustness_by_question: dict[str, int | None] = {}
    for qid in pages_by_id:
        judgements = judgements_by_question.get(qid, [])
        robs = [j.robustness for j in judgements if j.robustness is not None]
        robustness_by_question[qid] = max(robs) if robs else None

    lines: list[str] = []
    await _emit(
        root.id,
        prefix="",
        connector="",
        child_prefix="",
        depth=0,
        max_depth=max_depth,
        pages_by_id=pages_by_id,
        children_by_parent=children_by_parent,
        overflow_by_parent=overflow_by_parent,
        robustness_by_question=robustness_by_question,
        seen_on_path=set(),
        lines=lines,
        db=db,
    )
    return "\n".join(lines)


_BRANCH = "├── "
_LAST = "└── "
_PIPE = "│   "
_GAP = "    "


async def _emit(
    node_id: str,
    *,
    prefix: str,
    connector: str,
    child_prefix: str,
    depth: int,
    max_depth: int,
    pages_by_id: dict[str, Page],
    children_by_parent: dict[str, list[str]],
    overflow_by_parent: dict[str, int],
    robustness_by_question: dict[str, int | None],
    seen_on_path: set[str],
    lines: list[str],
    db: DB,
) -> None:
    if node_id in seen_on_path:
        lines.append(
            f"{prefix}{connector}[Q] `{node_id[:8]}` -- *** cycle detected ***"
        )
        return
    page = pages_by_id.get(node_id)
    if page is None:
        return
    headline = await format_page(
        page, PageDetail.HEADLINE, linked_detail=None, db=db
    )
    robustness = robustness_by_question.get(node_id)
    if robustness is not None:
        answer_note = f" (Answered at robustness {robustness}/5)"
    else:
        answer_note = " (Unanswered)"
    lines.append(f"{prefix}{connector}{headline}{answer_note}")

    visible_children = [
        cid for cid in children_by_parent.get(node_id, []) if cid in pages_by_id
    ]
    overflow = overflow_by_parent.get(node_id, 0)

    if depth >= max_depth:
        if overflow:
            lines.append(
                f"{child_prefix}{_LAST}({overflow} more sub-Q(s) not shown -- horizon)"
            )
        return

    seen_next = seen_on_path | {node_id}
    total = len(visible_children) + (1 if overflow else 0)
    for idx, child_id in enumerate(visible_children):
        is_last = idx == total - 1
        next_connector = _LAST if is_last else _BRANCH
        next_child_prefix = child_prefix + (_GAP if is_last else _PIPE)
        await _emit(
            child_id,
            prefix=child_prefix,
            connector=next_connector,
            child_prefix=next_child_prefix,
            depth=depth + 1,
            max_depth=max_depth,
            pages_by_id=pages_by_id,
            children_by_parent=children_by_parent,
            overflow_by_parent=overflow_by_parent,
            robustness_by_question=robustness_by_question,
            seen_on_path=seen_next,
            lines=lines,
            db=db,
        )
    if overflow:
        lines.append(
            f"{child_prefix}{_LAST}({overflow} more sub-Q(s) not shown -- horizon)"
        )
