"""Subgraph rendering and exploration tools.

Provides BFS-based subgraph renderers and a tool factory for LLM agents
that need to navigate the research graph.

Two rendering modes:
- **Questions only** (``render_question_subgraph``): walks CHILD_QUESTION
  links, showing only question pages.
- **Full graph** (``render_subgraph``): walks all link types (both
  directions, except SUPERSEDES) and shows all page types as equal nodes.

Both use level-by-level BFS with batched queries — O(depth) DB round
trips regardless of fan-out.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import BaseModel, Field

from rumil.context import format_page
from rumil.database import DB
from rumil.llm import Tool
from rumil.models import LinkType, Page, PageDetail, PageType
from rumil.settings import get_settings
from rumil.tracing.trace_events import RenderQuestionSubgraphEvent
from rumil.tracing.tracer import CallTrace

log = logging.getLogger(__name__)


_BRANCH = "├── "
_LAST = "└── "
_PIPE = "│   "
_GAP = "    "


@dataclass
class SubgraphResult:
    """Return value from subgraph rendering, carrying both text and metadata."""

    text: str
    root_page: Page | None = None


async def render_question_subgraph(
    page_id: str,
    db: DB,
    *,
    max_depth: int = 6,
    max_pages: int | None = None,
    exclude_ids: set[str] | None = None,
    include_impact: bool = False,
    global_impact: dict[str, float] | None = None,
) -> str:
    """Render a questions-only subgraph rooted at *page_id*.

    Walks CHILD_QUESTION links level-by-level, rendering each question as
    a single headline line indented by depth. Stops expanding when *either*
    ``max_depth`` is reached *or* the cumulative number of loaded question
    pages reaches ``max_pages``. Cycle-safe.

    Any page id in *exclude_ids* is pruned entirely. If the root itself is
    excluded, returns an empty string.

    O(depth) DB round trips regardless of fan-out.
    """
    result = await _render_subgraph_impl(
        page_id, db,
        max_depth=max_depth,
        max_pages=max_pages,
        exclude_ids=exclude_ids,
        include_impact=include_impact,
        global_impact=global_impact,
        questions_only=True,
    )
    return result.text


async def render_subgraph(
    page_id: str,
    db: DB,
    *,
    max_depth: int = 6,
    max_pages: int | None = None,
    exclude_ids: set[str] | None = None,
    include_impact: bool = False,
    global_impact: dict[str, float] | None = None,
) -> str:
    """Render a full subgraph rooted at *page_id*.

    Unlike ``render_question_subgraph``, this walks **all** link types
    (both outgoing and incoming, except SUPERSEDES) and treats all page
    types as equal nodes in the BFS. Claims, judgements, concepts, etc.
    are fully expandable — not just leaf nodes.

    O(depth) DB round trips regardless of fan-out.
    """
    result = await _render_subgraph_impl(
        page_id, db,
        max_depth=max_depth,
        max_pages=max_pages,
        exclude_ids=exclude_ids,
        include_impact=include_impact,
        global_impact=global_impact,
        questions_only=False,
    )
    return result.text


_SKIP_LINK_TYPES = frozenset({LinkType.SUPERSEDES})


async def _render_subgraph_impl(
    page_id: str,
    db: DB,
    *,
    max_depth: int,
    max_pages: int | None,
    exclude_ids: set[str] | None,
    include_impact: bool,
    global_impact: dict[str, float] | None,
    questions_only: bool,
    highlight_run_id: str | None = None,
) -> SubgraphResult:
    """Core BFS subgraph renderer shared by both public functions.

    When *questions_only* is True, walks only CHILD_QUESTION links from
    question pages and only loads question pages.

    When *questions_only* is False, walks all link types (both outgoing
    and incoming, except SUPERSEDES) and loads all page types. Every page
    is a first-class node in the BFS.
    """
    excluded = exclude_ids or set()
    resolved = await db.resolve_page_id(page_id)
    if resolved is None:
        return SubgraphResult(f'[Page "{page_id}" not found]')
    if resolved in excluded:
        return SubgraphResult("")
    root = await db.get_page(resolved)
    if root is None:
        return SubgraphResult(f'[Page "{page_id}" not found]')
    if questions_only and root.page_type != PageType.QUESTION:
        return SubgraphResult(
            f"[Page `{resolved[:8]}` is not a question "
            f"(type={root.page_type.value})]",
            root_page=root,
        )

    pages_by_id: dict[str, Page] = {root.id: root}
    children_by_parent: dict[str, list[str]] = {}
    overflow_by_parent: dict[str, int] = {}
    impact_by_child: dict[str, int | None] = {}
    link_run_id_by_edge: dict[tuple[str, str], str] = {}
    visited: set[str] = {root.id}

    frontier: list[str] = [root.id]
    for depth in range(max_depth + 1):
        if not frontier:
            break

        if questions_only:
            child_ids_by_parent, next_ids = _collect_question_children(
                frontier,
                await db.get_links_from_many(frontier),
                excluded,
                visited,
                impact_by_child if include_impact else None,
                link_run_id_by_edge if highlight_run_id else None,
            )
        else:
            child_ids_by_parent, next_ids = await _collect_all_neighbors(
                frontier, db, excluded, visited,
                impact_by_child if include_impact else None,
                link_run_id_by_edge if highlight_run_id else None,
            )

        if depth == max_depth:
            for parent_id, child_ids in child_ids_by_parent.items():
                unvisited = [c for c in child_ids if c not in visited]
                if unvisited:
                    overflow_by_parent[parent_id] = len(unvisited)
            break

        if not next_ids:
            for parent_id, child_ids in child_ids_by_parent.items():
                children_by_parent[parent_id] = child_ids
            break

        fetched = await db.get_pages_by_ids(list(next_ids))
        if questions_only:
            new_pages = {
                cid: fetched[cid]
                for cid in next_ids
                if fetched.get(cid) is not None
                and fetched[cid].page_type == PageType.QUESTION
            }
        else:
            new_pages = {
                cid: fetched[cid]
                for cid in next_ids
                if fetched.get(cid) is not None
            }

        if (
            max_pages is not None
            and len(pages_by_id) + len(new_pages) > max_pages
        ):
            for parent_id, child_ids in child_ids_by_parent.items():
                unvisited = [c for c in child_ids if c not in visited]
                if unvisited:
                    overflow_by_parent[parent_id] = len(unvisited)
            break

        for parent_id, child_ids in child_ids_by_parent.items():
            children_by_parent[parent_id] = child_ids
        for cid, page in new_pages.items():
            pages_by_id[cid] = page
            visited.add(cid)
        frontier = [cid for cid in next_ids if cid in pages_by_id]

    question_ids = [
        pid for pid, p in pages_by_id.items()
        if p.page_type == PageType.QUESTION
    ]
    judgements_by_question = await db.get_judgements_for_questions(question_ids)
    robustness_by_question: dict[str, int | None] = {}
    for qid in question_ids:
        judgements = judgements_by_question.get(qid, [])
        robs = [j.robustness for j in judgements if j.robustness is not None]
        robustness_by_question[qid] = max(robs) if robs else None

    lines: list[str] = []
    await _emit(
        root.id,
        parent_id=None,
        prefix="",
        connector="",
        child_prefix="",
        depth=0,
        max_depth=max_depth,
        pages_by_id=pages_by_id,
        children_by_parent=children_by_parent,
        overflow_by_parent=overflow_by_parent,
        robustness_by_question=robustness_by_question,
        impact_by_child=impact_by_child if include_impact else None,
        global_impact=global_impact,
        link_run_id_by_edge=link_run_id_by_edge if highlight_run_id else None,
        seen_on_path=set(),
        lines=lines,
        db=db,
        questions_only=questions_only,
        highlight_run_id=highlight_run_id,
    )
    return SubgraphResult("\n".join(lines), root_page=root)


def _collect_question_children(
    frontier: Sequence[str],
    links_by_parent: dict[str, list],
    excluded: set[str],
    visited: set[str],
    impact_by_child: dict[str, int | None] | None,
    link_run_id_by_edge: dict[tuple[str, str], str] | None = None,
) -> tuple[dict[str, list[str]], set[str]]:
    """Collect CHILD_QUESTION children for questions-only mode."""
    child_ids_by_parent: dict[str, list[str]] = {}
    next_ids: set[str] = set()
    for parent_id in frontier:
        child_links = [
            l for l in links_by_parent.get(parent_id, [])
            if l.link_type == LinkType.CHILD_QUESTION
            and l.to_page_id not in excluded
        ]
        child_ids = [l.to_page_id for l in child_links]
        child_ids_by_parent[parent_id] = child_ids
        if impact_by_child is not None:
            for l in child_links:
                if l.to_page_id not in impact_by_child:
                    impact_by_child[l.to_page_id] = getattr(
                        l, "impact_on_parent_question", None
                    )
        if link_run_id_by_edge is not None:
            for l in child_links:
                if l.run_id:
                    link_run_id_by_edge[(parent_id, l.to_page_id)] = l.run_id
        for cid in child_ids:
            if cid not in visited:
                next_ids.add(cid)
    return child_ids_by_parent, next_ids


async def _collect_all_neighbors(
    frontier: Sequence[str],
    db: DB,
    excluded: set[str],
    visited: set[str],
    impact_by_child: dict[str, int | None] | None,
    link_run_id_by_edge: dict[tuple[str, str], str] | None = None,
) -> tuple[dict[str, list[str]], set[str]]:
    """Collect all neighbors (both directions, all link types except SUPERSEDES)."""
    outgoing = await db.get_links_from_many(frontier)
    incoming = await db.get_links_to_many(frontier)

    child_ids_by_parent: dict[str, list[str]] = {}
    next_ids: set[str] = set()

    for parent_id in frontier:
        seen_children: set[str] = set()
        child_ids: list[str] = []

        for link in outgoing.get(parent_id, []):
            if link.link_type in _SKIP_LINK_TYPES:
                continue
            target = link.to_page_id
            if target in excluded or target in seen_children:
                continue
            seen_children.add(target)
            child_ids.append(target)
            if (
                impact_by_child is not None
                and link.link_type == LinkType.CHILD_QUESTION
                and target not in impact_by_child
            ):
                impact_by_child[target] = getattr(
                    link, "impact_on_parent_question", None
                )
            if link_run_id_by_edge is not None and link.run_id:
                link_run_id_by_edge[(parent_id, target)] = link.run_id

        for link in incoming.get(parent_id, []):
            if link.link_type in _SKIP_LINK_TYPES:
                continue
            source = link.from_page_id
            if source in excluded or source in seen_children:
                continue
            seen_children.add(source)
            child_ids.append(source)
            if link_run_id_by_edge is not None and link.run_id:
                link_run_id_by_edge[(parent_id, source)] = link.run_id

        child_ids_by_parent[parent_id] = child_ids
        for cid in child_ids:
            if cid not in visited:
                next_ids.add(cid)

    return child_ids_by_parent, next_ids


async def _emit(
    node_id: str,
    *,
    parent_id: str | None,
    prefix: str,
    connector: str,
    child_prefix: str,
    depth: int,
    max_depth: int,
    pages_by_id: dict[str, Page],
    children_by_parent: dict[str, list[str]],
    overflow_by_parent: dict[str, int],
    robustness_by_question: dict[str, int | None],
    impact_by_child: dict[str, int | None] | None,
    global_impact: dict[str, float] | None,
    link_run_id_by_edge: dict[tuple[str, str], str] | None,
    seen_on_path: set[str],
    lines: list[str],
    db: DB,
    questions_only: bool,
    highlight_run_id: str | None = None,
) -> None:
    if node_id in seen_on_path:
        lines.append(
            f"{prefix}{connector}`{node_id[:8]}` -- *** cycle detected ***"
        )
        return
    page = pages_by_id.get(node_id)
    if page is None:
        return
    headline = await format_page(
        page, PageDetail.HEADLINE, linked_detail=None, db=db,
        highlight_run_id=highlight_run_id,
    )

    annotations: list[str] = []
    if page.page_type == PageType.QUESTION:
        robustness = robustness_by_question.get(node_id)
        if robustness is not None:
            annotations.append(f"Answered at robustness {robustness}/5")
        else:
            annotations.append("Unanswered")
    if impact_by_child is not None and node_id in impact_by_child:
        impact_val = impact_by_child[node_id]
        if impact_val is not None:
            annotations.append(f"impact on parent: {impact_val}/10")
    if global_impact is not None and node_id in global_impact:
        annotations.append(f"impact on root: {global_impact[node_id]}/10")
    if (
        link_run_id_by_edge is not None
        and parent_id is not None
        and highlight_run_id
        and link_run_id_by_edge.get((parent_id, node_id)) == highlight_run_id
    ):
        annotations.append("LINKED BY THIS RUN")

    suffix = f" ({', '.join(annotations)})" if annotations else ""
    lines.append(f"{prefix}{connector}{headline}{suffix}")

    visible_children = [
        cid for cid in children_by_parent.get(node_id, [])
        if cid in pages_by_id
    ]
    overflow = overflow_by_parent.get(node_id, 0)

    if depth >= max_depth:
        if overflow:
            noun = "more sub-Q(s)" if questions_only else "more"
            lines.append(
                f"{child_prefix}{_LAST}"
                f"({overflow} {noun} not shown -- horizon)"
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
            parent_id=node_id,
            prefix=child_prefix,
            connector=next_connector,
            child_prefix=next_child_prefix,
            depth=depth + 1,
            max_depth=max_depth,
            pages_by_id=pages_by_id,
            children_by_parent=children_by_parent,
            overflow_by_parent=overflow_by_parent,
            robustness_by_question=robustness_by_question,
            impact_by_child=impact_by_child,
            global_impact=global_impact,
            link_run_id_by_edge=link_run_id_by_edge,
            seen_on_path=seen_next,
            lines=lines,
            db=db,
            questions_only=questions_only,
            highlight_run_id=highlight_run_id,
        )
    if overflow:
        noun = "more sub-Q(s)" if questions_only else "more"
        lines.append(
            f"{child_prefix}{_LAST}"
            f"({overflow} {noun} not shown -- horizon)"
        )


class _ExploreSubgraphInput(BaseModel):
    page_id: str = Field(
        description="Short ID (first 8 chars) or full UUID of a question page",
    )


def make_explore_subgraph_tool(
    db: DB,
    trace: CallTrace,
    *,
    max_pages: int | None = None,
    include_impact: bool = False,
    global_impact: dict[str, float] | None = None,
    questions_only: bool = True,
    exclude_ids: set[str] | None = None,
    highlight_run_id: str | None = None,
) -> Tool:
    """Build a subgraph exploration tool.

    Parameters control the rendering mode:
    - *questions_only*: True for question-tree only, False to include
      considerations and judgements as leaf nodes.
    - *include_impact*: show impact scores on child-question links.
    - *max_pages*: cap on question pages loaded (defaults to setting).
    - *exclude_ids*: page IDs to prune from the rendered tree.
    """
    captured_global_impact = global_impact
    effective_max = (
        max_pages
        if max_pages is not None
        else get_settings().explore_subgraph_default_max_pages
    )
    render = _render_subgraph_impl
    name = "explore_question_subgraph" if questions_only else "explore_subgraph"
    desc_suffix = (
        "Shows question headlines only."
        if questions_only
        else (
            "Shows questions with their attached considerations and "
            "judgements as leaf nodes."
        )
    )

    async def fn(args: dict) -> str:
        payload = _ExploreSubgraphInput.model_validate(args)
        result = await render(
            payload.page_id,
            db,
            max_depth=6,
            max_pages=effective_max,
            include_impact=include_impact,
            global_impact=captured_global_impact,
            questions_only=questions_only,
            exclude_ids=exclude_ids,
            highlight_run_id=highlight_run_id,
        )

        root = result.root_page
        await trace.record(
            RenderQuestionSubgraphEvent(
                page_id=root.id if root else payload.page_id,
                page_headline=root.headline if root else "",
                response=result.text,
            )
        )
        return result.text or f'[Page "{payload.page_id}" not found or not a question]'

    return Tool(
        name=name,
        description=(
            "Render a subtree of the research graph rooted at the given "
            "question, showing headlines and answer status. "
            f"{desc_suffix} "
            "Use this to drill into any question by its short ID.\n\n"
            "Reading the output:\n"
            "- A question with NO indented children below it is a **leaf** "
            "— it genuinely has no sub-questions. Do not call this tool "
            "on it expecting to find more.\n"
            "- `(N more sub-Q(s) not shown -- horizon)` means children "
            "exist but were truncated. Call this tool on the parent to "
            "expand them.\n\n"
            "Example:\n"
            "  ├── `a1b2c3d4` How does X work? (Unanswered)      ← has children below\n"
            "  │   ├── `e5f6a7b8` What is the mechanism? (Unanswered) ← leaf, no children\n"
            "  │   └── (3 more sub-Q(s) not shown -- horizon)     ← truncated, drill deeper"
        ),
        input_schema=_ExploreSubgraphInput.model_json_schema(),
        fn=fn,
    )
