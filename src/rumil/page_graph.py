"""
In-memory graph of pages and links for fast lookups.

Replaces hundreds of sequential DB round-trips with two bulk queries
(all pages + all links), then serves all read operations from memory.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rumil.database import DB
    from rumil.database import DB as PageSource

from rumil.models import (
    LinkType,
    Page,
    PageLink,
    PageType,
    Workspace,
)

log = logging.getLogger(__name__)


class PageGraph:
    """Bulk-loaded, read-only snapshot of workspace pages and links.

    Load once per call lifecycle via ``PageGraph.load(db)``, then pass to
    functions that would otherwise make recursive per-page DB queries
    (build_workspace_map, format_question_for_find_considerations, etc.).
    """

    def __init__(
        self,
        pages: Sequence[Page],
        links: Sequence[PageLink],
    ):
        self._pages: dict[str, Page] = {p.id: p for p in pages}
        self._links_to: dict[str, list[PageLink]] = {}
        self._links_from: dict[str, list[PageLink]] = {}
        page_ids = set(self._pages)
        for link in links:
            if link.from_page_id in page_ids or link.to_page_id in page_ids:
                self._links_to.setdefault(link.to_page_id, []).append(link)
                self._links_from.setdefault(link.from_page_id, []).append(link)

    @classmethod
    async def load(cls, db: "PageSource") -> "PageGraph":
        pages = await db.get_pages_slim(active_only=True)
        page_ids = {p.id for p in pages}
        links = await db.get_all_links(page_ids=page_ids)
        log.debug(
            "PageGraph.load: %d pages, %d links",
            len(pages),
            len(links),
        )
        return cls(pages, links)

    async def get_page(self, page_id: str) -> Page | None:
        return self._pages.get(page_id)

    async def resolve_supersession_chain(
        self,
        page: Page,
    ) -> Page | None:
        """Try to resolve the supersession chain for a superseded *page*.

        PageGraph only holds active pages, so this can only resolve chains
        where the direct ``superseded_by`` target is active and present in
        the graph. Returns ``None`` otherwise — callers should fall back to
        ``DB.resolve_supersession_chain``.
        """
        if not page.is_superseded or not page.superseded_by:
            return None
        target = self._pages.get(page.superseded_by)
        if target and not target.is_superseded:
            return target
        return None

    async def get_pages(
        self,
        workspace: Workspace | None = None,
        page_type: PageType | None = None,
        active_only: bool = True,
    ) -> list[Page]:
        result = list(self._pages.values())
        if workspace:
            result = [p for p in result if p.workspace == workspace]
        if page_type:
            result = [p for p in result if p.page_type == page_type]
        if active_only:
            result = [p for p in result if p.is_active()]
        result.sort(key=lambda p: p.created_at, reverse=True)
        return result

    async def get_links_to(self, page_id: str) -> list[PageLink]:
        return list(self._links_to.get(page_id, []))

    async def get_links_from(self, page_id: str) -> list[PageLink]:
        return list(self._links_from.get(page_id, []))

    async def get_considerations_for_question(
        self,
        question_id: str,
    ) -> list[tuple[Page, PageLink]]:
        links = self._links_to.get(question_id, [])
        result = []
        for link in links:
            if link.link_type != LinkType.CONSIDERATION:
                continue
            page = self._pages.get(link.from_page_id)
            if page and page.is_active():
                result.append((page, link))
        return result

    async def get_dependents(
        self,
        page_id: str,
    ) -> list[tuple[Page, PageLink]]:
        """Pages that depend on *page_id* via DEPENDS_ON links."""
        result = []
        for link in self._links_to.get(page_id, []):
            if link.link_type != LinkType.DEPENDS_ON:
                continue
            page = self._pages.get(link.from_page_id)
            if page and page.is_active():
                result.append((page, link))
        return result

    async def get_dependencies(
        self,
        page_id: str,
    ) -> list[tuple[Page, PageLink]]:
        """Pages that *page_id* depends on via DEPENDS_ON links."""
        result = []
        for link in self._links_from.get(page_id, []):
            if link.link_type != LinkType.DEPENDS_ON:
                continue
            page = self._pages.get(link.to_page_id)
            if page:
                result.append((page, link))
        return result

    async def get_judgements_for_question(
        self,
        question_id: str,
    ) -> list[Page]:
        links = self._links_to.get(question_id, [])
        result = []
        for link in links:
            if link.link_type != LinkType.RELATED:
                continue
            page = self._pages.get(link.from_page_id)
            if page and page.is_active() and page.page_type == PageType.JUDGEMENT:
                result.append(page)
        return result

    async def get_judgements_for_questions(
        self,
        question_ids: Sequence[str],
    ) -> dict[str, list[Page]]:
        """Bulk variant of get_judgements_for_question for many questions."""
        result: dict[str, list[Page]] = {qid: [] for qid in question_ids}
        for qid in question_ids:
            for link in self._links_to.get(qid, []):
                if link.link_type != LinkType.RELATED:
                    continue
                page = self._pages.get(link.from_page_id)
                if page and page.is_active() and page.page_type == PageType.JUDGEMENT:
                    result[qid].append(page)
        return result

    async def get_parent_question(self, question_id: str) -> Page | None:
        """Return the parent question, or None if this is a root question."""
        for link in self._links_to.get(question_id, []):
            if link.link_type == LinkType.CHILD_QUESTION:
                page = self._pages.get(link.from_page_id)
                if page and page.is_active():
                    return page
        return None

    async def get_child_questions(self, parent_id: str) -> list[Page]:
        links = self._links_from.get(parent_id, [])
        result = []
        for link in links:
            if link.link_type != LinkType.CHILD_QUESTION:
                continue
            page = self._pages.get(link.to_page_id)
            if page and page.is_active():
                result.append(page)
        return result

    async def get_child_questions_with_links(
        self,
        parent_id: str,
    ) -> list[tuple[Page, PageLink]]:
        links = self._links_from.get(parent_id, [])
        result = []
        for link in links:
            if link.link_type != LinkType.CHILD_QUESTION:
                continue
            page = self._pages.get(link.to_page_id)
            if page and page.is_active():
                result.append((page, link))
        return result

    async def get_root_questions(
        self,
        workspace: Workspace = Workspace.RESEARCH,
    ) -> list[Page]:
        child_ids: set[str] = set()
        for links in self._links_from.values():
            for link in links:
                if link.link_type == LinkType.CHILD_QUESTION:
                    child_ids.add(link.to_page_id)
        return [
            p
            for p in self._pages.values()
            if p.page_type == PageType.QUESTION
            and p.workspace == workspace
            and p.is_active()
            and p.id not in child_ids
        ]

    async def get_last_find_considerations_info(
        self,
        question_id: str,
    ) -> tuple[str, int | None] | None:
        return None

    async def get_ingest_history(self) -> dict[str, list[str]]:
        return {}


class SubtreeGraph(PageGraph):
    """Scope-rooted graph view loaded with O(depth) batched round trips.

    Walks ``CHILD_QUESTION`` edges down from ``root_id`` level-by-level via
    ``get_links_from_many`` + ``get_pages_by_ids``. Optionally walks ancestors
    upward and includes their direct children (siblings of the scope at each
    level). For every visited question, all incident links and link-endpoint
    pages are bulk-fetched, so the resulting view supports the same read
    surface as ``PageGraph`` (considerations, judgements, dependents,
    dependencies, parent/child questions) — but only for the loaded subset.

    Round-trip cost: ``O(descend_depth + ancestor_depth)`` regardless of
    fan-out, plus a constant number of bulk queries to materialize incident
    links and endpoint pages.
    """

    @classmethod
    async def load_for_root(
        cls,
        db: "DB",
        root_id: str,
        *,
        descend_max_depth: int = 20,
        include_ancestors: bool = False,
        include_ancestor_children: bool = False,
    ) -> "SubtreeGraph":
        root = await db.get_page(root_id)
        if root is None:
            log.debug("SubtreeGraph.load: root %s not found", root_id[:8])
            return cls([], [])

        question_ids: set[str] = {root_id}

        await _bfs_descend_questions(
            db,
            seeds=[root_id],
            visited=question_ids,
            max_depth=descend_max_depth,
        )

        if include_ancestors:
            ancestor_ids = await _walk_ancestor_questions(db, root_id)
            question_ids.update(ancestor_ids)
            if include_ancestor_children:
                await _bfs_descend_questions(
                    db,
                    seeds=list(ancestor_ids),
                    visited=question_ids,
                    max_depth=1,
                )

        pages_by_id = await db.get_pages_by_ids(list(question_ids))

        in_links_map = await db.get_links_to_many(list(question_ids))
        out_links_map = await db.get_links_from_many(list(question_ids))

        seen_link_ids: set[str] = set()
        links: list[PageLink] = []
        for link_lists in (in_links_map.values(), out_links_map.values()):
            for ls in link_lists:
                for link in ls:
                    if link.id in seen_link_ids:
                        continue
                    seen_link_ids.add(link.id)
                    links.append(link)

        endpoint_ids: set[str] = set()
        for link in links:
            if link.from_page_id not in pages_by_id:
                endpoint_ids.add(link.from_page_id)
            if link.to_page_id not in pages_by_id:
                endpoint_ids.add(link.to_page_id)
        if endpoint_ids:
            extra_pages = await db.get_pages_by_ids(list(endpoint_ids))
            pages_by_id.update(extra_pages)

        active_pages = [p for p in pages_by_id.values() if p.is_active()]

        log.debug(
            "SubtreeGraph.load: root=%s, %d questions, %d pages, %d links",
            root_id[:8],
            len(question_ids),
            len(active_pages),
            len(links),
        )
        return cls(active_pages, links)


async def _bfs_descend_questions(
    db: "DB",
    *,
    seeds: Sequence[str],
    visited: set[str],
    max_depth: int,
) -> None:
    """Walk CHILD_QUESTION edges down from seeds, mutating *visited* in place."""
    if not seeds or max_depth <= 0:
        return
    frontier: list[str] = [sid for sid in seeds]
    for _ in range(max_depth):
        if not frontier:
            return
        links_by_parent = await db.get_links_from_many(frontier)
        next_ids: set[str] = set()
        for parent_id in frontier:
            for link in links_by_parent.get(parent_id, []):
                if link.link_type != LinkType.CHILD_QUESTION:
                    continue
                if link.to_page_id in visited:
                    continue
                next_ids.add(link.to_page_id)
        if not next_ids:
            return
        visited.update(next_ids)
        frontier = list(next_ids)


async def _walk_ancestor_questions(db: "DB", start_id: str) -> list[str]:
    """Walk CHILD_QUESTION edges upward from start_id, returning ancestor IDs.

    Cycle-safe; stops when no parent exists or a cycle is detected.
    """
    ancestors: list[str] = []
    visited: set[str] = {start_id}
    current = start_id
    for _ in range(64):  # safety bound
        in_links = await db.get_links_to_many([current])
        parent_links = [
            link
            for link in in_links.get(current, [])
            if link.link_type == LinkType.CHILD_QUESTION
        ]
        if not parent_links:
            return ancestors
        parent_id = parent_links[0].from_page_id
        if parent_id in visited:
            return ancestors
        visited.add(parent_id)
        ancestors.append(parent_id)
        current = parent_id
    return ancestors
