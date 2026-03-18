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
    (build_workspace_map, format_question_for_scout, etc.).
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
        pages = await db.get_pages(active_only=True)
        links = await db.get_all_links()
        log.debug(
            'PageGraph.load: %d pages, %d links', len(pages), len(links),
        )
        return cls(pages, links)

    async def get_page(self, page_id: str) -> Page | None:
        return self._pages.get(page_id)

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
        self, question_id: str,
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

    async def get_judgements_for_question(
        self, question_id: str,
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
        self, parent_id: str,
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
        self, workspace: Workspace = Workspace.RESEARCH,
    ) -> list[Page]:
        child_ids: set[str] = set()
        for links in self._links_from.values():
            for link in links:
                if link.link_type == LinkType.CHILD_QUESTION:
                    child_ids.add(link.to_page_id)
        return [
            p for p in self._pages.values()
            if p.page_type == PageType.QUESTION
            and p.workspace == workspace
            and p.is_active()
            and p.id not in child_ids
        ]

    async def get_last_scout_info(
        self, question_id: str,
    ) -> tuple[str, int | None] | None:
        return None

    async def get_ingest_history(self) -> dict[str, list[str]]:
        return {}
