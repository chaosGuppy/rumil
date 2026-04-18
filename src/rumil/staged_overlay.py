"""StagedOverlay: pairs ``_staged_filter`` with ``_apply_page_events``/``_apply_link_events``.

This is a prototype. Most of ``database.py`` still uses the raw filter + apply
pattern directly; this abstraction is intended to eventually replace those ~60
call sites with a single audit point.

The staged-runs feature requires two independent operations on every page/link
read: (1) a visibility filter on the SQL query, and (2) a replay of mutation
events over the returned rows. Forgetting either half silently breaks staged
isolation. ``StagedOverlay`` pairs them so callers can't accidentally split them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rumil.models import Page, PageLink

if TYPE_CHECKING:
    from rumil.database import DB


class StagedOverlay:
    """Pairs the two halves of staged-run visibility.

    Callers build a base query, hand it over, and receive Pages/Links that
    already have both the staged filter applied and the mutation events
    overlaid. Using this guarantees the two halves can't drift apart.
    """

    def __init__(self, db: DB):
        self._db = db

    async def read_pages(self, query: Any) -> list[Page]:
        """Run *query* as a pages read, applying staged visibility + events."""
        from rumil.database import _row_to_page, _rows

        query = self._db._staged_filter(query)
        rows = _rows(await self._db._execute(query))
        pages = [_row_to_page(r) for r in rows]
        return await self._db._apply_page_events(pages)

    async def read_page_opt(self, query: Any) -> Page | None:
        """Single-row variant of :meth:`read_pages`. Returns None if empty."""
        pages = await self.read_pages(query)
        return pages[0] if pages else None

    async def read_links(self, query: Any) -> list[PageLink]:
        """Run *query* as a page_links read, applying staged visibility + events."""
        from rumil.database import _row_to_link, _rows

        query = self._db._staged_filter(query)
        rows = _rows(await self._db._execute(query))
        links = [_row_to_link(r) for r in rows]
        return await self._db._apply_link_events(links)
