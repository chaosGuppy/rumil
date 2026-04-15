"""BFS graph exploration tool for the evaluation agent.

Produces a tiered text view of the local graph around a page:
- Pages within N hops: full content
- Pages N+1..M hops: abstract only
- Pages M+1..O hops: headline only
- Frontier connections beyond O hops are indicated but not expanded.
"""

from collections.abc import Sequence

from rumil.context import format_page
from rumil.database import DB
from rumil.models import Page, PageDetail, PageLink
from rumil.settings import get_settings


async def explore_page_impl(
    page_id: str,
    db: DB,
    highlight_run_id: str | None = None,
) -> str:
    """Explore the local graph around *page_id* and return formatted text.

    Hop-distance thresholds are read from settings:
    - evaluate_content_hops  (N, default 0)
    - evaluate_abstract_hops (M, default 1)
    - evaluate_headline_hops (O, default 2)
    """
    settings = get_settings()
    n = settings.evaluate_content_hops
    m = settings.evaluate_abstract_hops
    o = settings.evaluate_headline_hops

    resolved = await db.resolve_page_id(page_id)
    if resolved is None:
        return f'[Page "{page_id}" not found]'

    visited, neighbor_map = await _bfs(resolved, o, db)

    all_ids = set(visited.keys())
    page_map = await db.get_pages_by_ids(list(all_ids))
    all_links = await db.get_all_links(all_ids)

    link_index = _build_link_index(all_links, all_ids)

    frontier = _detect_frontier(visited, neighbor_map, o)

    content_ids = [pid for pid, d in visited.items() if d <= n]
    abstract_ids = [pid for pid, d in visited.items() if n < d <= m]
    headline_ids = [pid for pid, d in visited.items() if m < d <= o]

    parts: list[str] = []

    if content_ids:
        parts.append("=== Full-detail pages ===")
        parts.append("")
        for pid in content_ids:
            page = page_map.get(pid)
            if not page:
                continue
            parts.append(
                await format_page(
                    page,
                    PageDetail.CONTENT,
                    linked_detail=None,
                    db=db,
                    highlight_run_id=highlight_run_id,
                )
            )
            parts.append("")
            parts.extend(
                _render_links(
                    pid,
                    link_index,
                    page_map,
                    visited,
                    highlight_run_id=highlight_run_id,
                )
            )
            parts.append("")

    if abstract_ids:
        parts.append("--- Abstract-level pages ---")
        parts.append("")
        for pid in abstract_ids:
            page = page_map.get(pid)
            if not page:
                continue
            parts.append(
                await format_page(
                    page,
                    PageDetail.ABSTRACT,
                    linked_detail=None,
                    db=db,
                    highlight_run_id=highlight_run_id,
                )
            )
            parts.append("")
            parts.extend(
                _render_links(
                    pid,
                    link_index,
                    page_map,
                    visited,
                    highlight_run_id=highlight_run_id,
                )
            )
            parts.append("")

    if headline_ids:
        parts.append("--- Headline-level pages ---")
        parts.append("")
        for pid in headline_ids:
            page = page_map.get(pid)
            if not page:
                continue
            parts.append(
                await format_page(
                    page,
                    PageDetail.HEADLINE,
                    linked_detail=None,
                    db=db,
                    highlight_run_id=highlight_run_id,
                )
            )
            frontier_for = frontier.get(pid, set())
            if frontier_for:
                parts.append(
                    f"  ↳ {len(frontier_for)} further connection(s) beyond this horizon"
                )
            parts.append("")

    if not parts:
        return f'[No pages found around "{page_id}"]'

    return "\n".join(parts).rstrip()


async def _bfs(
    start_id: str,
    max_hops: int,
    db: DB,
) -> tuple[dict[str, int], dict[str, set[str]]]:
    """BFS from *start_id* up to *max_hops*.

    Issues two batched link queries (links_from + links_to) per BFS level,
    so total round trips are O(max_hops) regardless of fan-out.

    Returns (visited, neighbor_map) where:
    - visited maps page_id → minimum hop distance
    - neighbor_map maps page_id → set of all neighbor page_ids (including beyond max_hops)
    """
    visited: dict[str, int] = {start_id: 0}
    neighbor_map: dict[str, set[str]] = {}
    frontier: list[str] = [start_id]
    dist = 0

    while frontier and dist <= max_hops:
        links_from_map = await db.get_links_from_many(frontier)
        links_to_map = await db.get_links_to_many(frontier)

        next_frontier: list[str] = []
        for pid in frontier:
            neighbors: set[str] = set()
            for link in links_from_map.get(pid, []):
                neighbors.add(link.to_page_id)
            for link in links_to_map.get(pid, []):
                neighbors.add(link.from_page_id)
            neighbor_map[pid] = neighbors

            if dist < max_hops:
                for neighbor_id in neighbors:
                    if neighbor_id not in visited:
                        visited[neighbor_id] = dist + 1
                        next_frontier.append(neighbor_id)

        frontier = next_frontier
        dist += 1

    return visited, neighbor_map


def _build_link_index(
    links: Sequence[PageLink],
    page_ids: set[str],
) -> dict[str, list[tuple[PageLink, str]]]:
    """Build an index from page_id → [(link, direction)] for rendering.

    *direction* is 'out' or 'in' indicating whether the link goes from or to
    the indexed page.  Only links where both endpoints are in *page_ids* are
    included.
    """
    index: dict[str, list[tuple[PageLink, str]]] = {}
    for link in links:
        if link.from_page_id in page_ids and link.to_page_id in page_ids:
            index.setdefault(link.from_page_id, []).append((link, "out"))
            index.setdefault(link.to_page_id, []).append((link, "in"))
    return index


def _detect_frontier(
    visited: dict[str, int],
    neighbor_map: dict[str, set[str]],
    max_hops: int,
) -> dict[str, set[str]]:
    """For each page at *max_hops*, find neighbors that are beyond the horizon."""
    frontier: dict[str, set[str]] = {}
    for pid, dist in visited.items():
        if dist == max_hops:
            neighbors = neighbor_map.get(pid, set())
            beyond = neighbors - set(visited.keys())
            if beyond:
                frontier[pid] = beyond
    return frontier


def _render_links(
    page_id: str,
    link_index: dict[str, list[tuple[PageLink, str]]],
    page_map: dict[str, Page],
    visited: dict[str, int],
    highlight_run_id: str | None = None,
) -> list[str]:
    """Render the links for a single page as indented text lines."""
    entries = link_index.get(page_id, [])
    if not entries:
        return []

    lines: list[str] = ["  Links:"]
    seen: set[str] = set()

    for link, direction in entries:
        if direction == "out":
            other_id = link.to_page_id
            arrow = "→"
        else:
            other_id = link.from_page_id
            arrow = "←"

        dedup_key = f"{link.id}"
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        other = page_map.get(other_id)
        other_label = (
            f'`{other_id[:8]}` "{other.headline}"' if other else f"`{other_id[:8]}`"
        )

        link_desc = link.link_type.value.upper()
        extras: list[str] = []
        if link.direction:
            extras.append(link.direction.value)
        if link.strength is not None:
            extras.append(f"{link.strength:.1f}/5")
        if extras:
            link_desc += f" ({', '.join(extras)})"

        added_tag = ""
        if highlight_run_id and link.run_id and link.run_id == highlight_run_id:
            added_tag = " [ADDED BY THIS RUN]"
        lines.append(
            f"  {arrow} {link_desc} [link:{link.id[:8]}]: {other_label}{added_tag}"
        )

    return lines
