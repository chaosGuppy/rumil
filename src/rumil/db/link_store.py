"""LinkStore: everything keyed on the ``page_links`` table.

Owns link CRUD (``save_link``, ``delete_link``, ``update_link_role``),
traversals (``get_links_to``, ``get_links_from``, bulk ``_many``
variants), relationship-specific helpers (considerations, judgements,
tension verdicts, dependents/dependencies, child questions, views,
inlays, view items), and supersession-magnitude lookups on the
mutation-events log.

**Mutation-log invariant preserved**: ``delete_link`` and
``update_link_role`` record a ``mutation_events`` row via
``self._db.record_mutation_event`` first, then dual-write only when
``not staged`` — same pattern as ``PageStore.supersede_page`` /
``update_page_content``.
"""

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from rumil.db.row_helpers import _LINK_COLUMNS, _row_to_link, _row_to_page, _rows
from rumil.models import LinkRole, LinkType, Page, PageLink, PageType
from rumil.settings import get_settings

if TYPE_CHECKING:
    from rumil.database import DB


log = logging.getLogger(__name__)


class LinkStore:
    """Reads and mutation-logged writes on the ``page_links`` table."""

    def __init__(self, db: "DB") -> None:
        self._db = db

    @property
    def client(self) -> Any:
        return self._db.client

    async def save_link(self, link: PageLink) -> None:
        if get_settings().dedupe_page_links:
            existing = await self._find_duplicate_link(link)
            if existing is not None and existing.id != link.id:
                log.debug(
                    "save_link: dedup, existing link %s matches (from=%s to=%s type=%s)",
                    existing.id,
                    link.from_page_id[:8],
                    link.to_page_id[:8],
                    link.link_type.value,
                )
                return
        await self._db._execute(
            self.client.table("page_links").upsert(
                {
                    "id": link.id,
                    "from_page_id": link.from_page_id,
                    "to_page_id": link.to_page_id,
                    "link_type": link.link_type.value,
                    "direction": link.direction.value if link.direction else None,
                    "strength": link.strength,
                    "reasoning": link.reasoning,
                    "role": link.role.value,
                    "importance": link.importance,
                    "section": link.section,
                    "position": link.position,
                    "impact_on_parent_question": link.impact_on_parent_question,
                    "created_at": link.created_at.isoformat(),
                    "run_id": self._db.run_id,
                    "staged": self._db.staged,
                }
            )
        )

    async def _find_duplicate_link(self, link: PageLink) -> PageLink | None:
        """Return an existing link with the same (from, to, link_type) if one is
        already visible to this DB handle, else None."""
        query = (
            self.client.table("page_links")
            .select("*")
            .eq("from_page_id", link.from_page_id)
            .eq("to_page_id", link.to_page_id)
            .eq("link_type", link.link_type.value)
            .limit(1)
        )
        query = self._db._staged_filter(query)
        rows = _rows(await self._db._execute(query))
        if not rows:
            return None
        applied = await self._db._apply_link_events([_row_to_link(rows[0])])
        return applied[0] if applied else None

    async def get_link(self, link_id: str) -> PageLink | None:
        query = self.client.table("page_links").select("*").eq("id", link_id)
        query = self._db._staged_filter(query)
        rows = _rows(await self._db._execute(query))
        if not rows:
            return None
        links = await self._db._apply_link_events([_row_to_link(rows[0])])
        return links[0] if links else None

    async def get_links_to(self, page_id: str) -> list[PageLink]:
        query = self.client.table("page_links").select("*").eq("to_page_id", page_id)
        query = self._db._staged_filter(query)
        rows = _rows(await self._db._execute(query))
        return await self._db._apply_link_events([_row_to_link(r) for r in rows])

    async def get_view_for_question(self, question_id: str) -> Page | None:
        """Find the active (non-superseded) View page for a question."""
        query = (
            self.client.table("page_links")
            .select(_LINK_COLUMNS)
            .eq("to_page_id", question_id)
            .eq("link_type", LinkType.VIEW_OF.value)
        )
        query = self._db._staged_filter(query)
        rows = _rows(await self._db._execute(query))
        links = await self._db._apply_link_events([_row_to_link(r) for r in rows])
        if not links:
            return None
        view_ids = [link.from_page_id for link in links]
        pages = await self._db.get_pages_by_ids(view_ids)
        for view_id in view_ids:
            page = pages.get(view_id)
            if page and not page.is_superseded:
                return page
        return None

    async def get_inlays_for_question(self, question_id: str) -> list[Page]:
        """Return active INLAY pages bound to a question."""
        query = (
            self.client.table("page_links")
            .select(_LINK_COLUMNS)
            .eq("to_page_id", question_id)
            .eq("link_type", LinkType.INLAY_OF.value)
        )
        query = self._db._staged_filter(query)
        rows = _rows(await self._db._execute(query))
        links = await self._db._apply_link_events([_row_to_link(r) for r in rows])
        if not links:
            return []
        inlay_ids = [link.from_page_id for link in links]
        pages = await self._db.get_pages_by_ids(inlay_ids)
        active: list[Page] = []
        for inlay_id in inlay_ids:
            page = pages.get(inlay_id)
            if page and page.page_type == PageType.INLAY and not page.is_superseded:
                active.append(page)
        active.sort(key=lambda p: p.created_at, reverse=True)
        return active

    async def get_views_for_questions(
        self,
        question_ids: Sequence[str],
    ) -> dict[str, Page | None]:
        """Bulk-fetch the active View page for many questions."""
        result: dict[str, Page | None] = {qid: None for qid in question_ids}
        if not question_ids:
            return result
        id_list = list(dict.fromkeys(question_ids))
        links_by_target = await self.get_links_to_many(id_list)
        view_from_ids: list[str] = []
        view_links_by_question: dict[str, list[PageLink]] = {}
        for qid in id_list:
            qlinks = [l for l in links_by_target.get(qid, []) if l.link_type == LinkType.VIEW_OF]
            if qlinks:
                view_links_by_question[qid] = qlinks
                view_from_ids.extend(l.from_page_id for l in qlinks)
        if not view_from_ids:
            return result
        pages = await self._db.get_pages_by_ids(list(dict.fromkeys(view_from_ids)))
        for qid, qlinks in view_links_by_question.items():
            for link in qlinks:
                page = pages.get(link.from_page_id)
                if page and not page.is_superseded:
                    result[qid] = page
                    break
        return result

    async def get_view_items(
        self,
        view_id: str,
        min_importance: int | None = None,
    ) -> list[tuple[Page, PageLink]]:
        """Get VIEW_ITEM pages linked to a View, with their link metadata."""
        links = await self.get_links_from(view_id)
        item_links = [link for link in links if link.link_type == LinkType.VIEW_ITEM]
        if min_importance is not None:
            item_links = [
                link
                for link in item_links
                if link.importance is not None and link.importance >= min_importance
            ]
        if not item_links:
            return []
        item_ids = [link.to_page_id for link in item_links]
        pages_by_id = await self._db.get_pages_by_ids(item_ids)

        view_page = await self._db.get_page(view_id)
        section_order: dict[str, int] = {}
        if view_page and view_page.sections:
            section_order = {s: i for i, s in enumerate(view_page.sections)}

        results: list[tuple[Page, PageLink]] = []
        for link in item_links:
            page = pages_by_id.get(link.to_page_id)
            if page and not page.is_superseded:
                results.append((page, link))
        results.sort(
            key=lambda pair: (
                section_order.get(pair[1].section or "", 999),
                pair[1].position or 0,
            )
        )
        return results

    async def get_links_from(self, page_id: str) -> list[PageLink]:
        query = self.client.table("page_links").select("*").eq("from_page_id", page_id)
        return await self._db.overlay.read_links(query)

    async def get_links_from_many(
        self,
        page_ids: Sequence[str],
    ) -> dict[str, list[PageLink]]:
        """Bulk-fetch outgoing links for many pages. Returns {page_id: [links]}."""
        result: dict[str, list[PageLink]] = {pid: [] for pid in page_ids}
        if not page_ids:
            return result
        id_list = list(dict.fromkeys(page_ids))
        batch_size = 100
        page_size = 2000
        all_links: list[PageLink] = []
        for start in range(0, len(id_list), batch_size):
            batch = id_list[start : start + batch_size]
            offset = 0
            while True:
                query = (
                    self.client.table("page_links").select(_LINK_COLUMNS).in_("from_page_id", batch)
                )
                query = self._db._staged_filter(query)
                rows = _rows(await self._db._execute(query.range(offset, offset + page_size - 1)))
                all_links.extend(_row_to_link(r) for r in rows)
                if len(rows) < page_size:
                    break
                offset += page_size
        applied = await self._db._apply_link_events(all_links)
        for link in applied:
            result.setdefault(link.from_page_id, []).append(link)
        return result

    async def get_links_to_many(
        self,
        page_ids: Sequence[str],
    ) -> dict[str, list[PageLink]]:
        """Bulk-fetch incoming links for many pages. Returns {page_id: [links]}."""
        result: dict[str, list[PageLink]] = {pid: [] for pid in page_ids}
        if not page_ids:
            return result
        id_list = list(dict.fromkeys(page_ids))
        batch_size = 100
        page_size = 2000
        all_links: list[PageLink] = []
        for start in range(0, len(id_list), batch_size):
            batch = id_list[start : start + batch_size]
            offset = 0
            while True:
                query = (
                    self.client.table("page_links").select(_LINK_COLUMNS).in_("to_page_id", batch)
                )
                query = self._db._staged_filter(query)
                rows = _rows(await self._db._execute(query.range(offset, offset + page_size - 1)))
                all_links.extend(_row_to_link(r) for r in rows)
                if len(rows) < page_size:
                    break
                offset += page_size
        applied = await self._db._apply_link_events(all_links)
        for link in applied:
            result.setdefault(link.to_page_id, []).append(link)
        return result

    async def get_considerations_for_question(
        self,
        question_id: str,
    ) -> list[tuple[Page, PageLink]]:
        """Return (claim_page, link) pairs for all considerations on a question."""
        links = await self.get_links_to(question_id)
        consideration_links = [l for l in links if l.link_type == LinkType.CONSIDERATION]
        if not consideration_links:
            return []
        pages = await self._db.get_pages_by_ids([l.from_page_id for l in consideration_links])
        return [
            (pages[l.from_page_id], l)
            for l in consideration_links
            if l.from_page_id in pages and pages[l.from_page_id].is_active()
        ]

    async def get_considerations_for_questions(
        self,
        question_ids: Sequence[str],
    ) -> dict[str, list[tuple[Page, PageLink]]]:
        """Bulk-fetch considerations for many questions."""
        result: dict[str, list[tuple[Page, PageLink]]] = {qid: [] for qid in question_ids}
        if not question_ids:
            return result
        id_list = list(dict.fromkeys(question_ids))
        links_by_target = await self.get_links_to_many(id_list)
        consideration_links: list[PageLink] = []
        for qid in id_list:
            for link in links_by_target.get(qid, []):
                if link.link_type == LinkType.CONSIDERATION:
                    consideration_links.append(link)
        if not consideration_links:
            return result
        page_ids = list({l.from_page_id for l in consideration_links})
        pages = await self._db.get_pages_by_ids(page_ids)
        for link in consideration_links:
            page = pages.get(link.from_page_id)
            if page and page.is_active():
                result[link.to_page_id].append((page, link))
        return result

    async def get_parent_question(self, question_id: str) -> Page | None:
        """Return the parent question, or None if this is a root question."""
        links = await self.get_links_to(question_id)
        for link in links:
            if link.link_type == LinkType.CHILD_QUESTION:
                page = await self._db.get_page(link.from_page_id)
                if page and page.is_active():
                    return page
        return None

    async def get_child_questions(self, parent_id: str) -> list[Page]:
        """Return sub-questions of a question."""
        links = await self.get_links_from(parent_id)
        child_links = [l for l in links if l.link_type == LinkType.CHILD_QUESTION]
        if not child_links:
            return []
        pages = await self._db.get_pages_by_ids([l.to_page_id for l in child_links])
        return [
            pages[l.to_page_id]
            for l in child_links
            if l.to_page_id in pages and pages[l.to_page_id].is_active()
        ]

    async def get_child_questions_with_links(
        self,
        parent_id: str,
    ) -> list[tuple[Page, PageLink]]:
        """Return (child_page, link) pairs for sub-questions of a question."""
        links = await self.get_links_from(parent_id)
        child_links = [l for l in links if l.link_type == LinkType.CHILD_QUESTION]
        if not child_links:
            return []
        pages = await self._db.get_pages_by_ids([l.to_page_id for l in child_links])
        return [
            (pages[l.to_page_id], l)
            for l in child_links
            if l.to_page_id in pages and pages[l.to_page_id].is_active()
        ]

    async def get_judgements_for_question(self, question_id: str) -> list[Page]:
        links = await self.get_links_to(question_id)
        judgement_links = [l for l in links if l.link_type == LinkType.ANSWERS]
        if not judgement_links:
            return []
        pages = await self._db.get_pages_by_ids([l.from_page_id for l in judgement_links])
        return [
            pages[l.from_page_id]
            for l in judgement_links
            if l.from_page_id in pages
            and pages[l.from_page_id].is_active()
            and pages[l.from_page_id].page_type == PageType.JUDGEMENT
        ]

    async def get_judgements_for_questions(
        self,
        question_ids: Sequence[str],
    ) -> dict[str, list[Page]]:
        """Bulk-fetch active judgements for many questions."""
        result: dict[str, list[Page]] = {qid: [] for qid in question_ids}
        if not question_ids:
            return result
        id_list = list(dict.fromkeys(question_ids))
        batch_size = 100
        page_size = 2000
        all_links: list[PageLink] = []
        for start in range(0, len(id_list), batch_size):
            batch = id_list[start : start + batch_size]
            offset = 0
            while True:
                query = (
                    self.client.table("page_links")
                    .select(_LINK_COLUMNS)
                    .in_("to_page_id", batch)
                    .eq("link_type", LinkType.ANSWERS.value)
                )
                query = self._db._staged_filter(query)
                rows = _rows(await self._db._execute(query.range(offset, offset + page_size - 1)))
                all_links.extend(_row_to_link(r) for r in rows)
                if len(rows) < page_size:
                    break
                offset += page_size
        applied = await self._db._apply_link_events(all_links)
        from_ids = list({l.from_page_id for l in applied})
        pages = await self._db.get_pages_by_ids(from_ids)
        for link in applied:
            page = pages.get(link.from_page_id)
            if page is not None and page.is_active() and page.page_type == PageType.JUDGEMENT:
                result.setdefault(link.to_page_id, []).append(page)
        return result

    async def get_tension_verdicts_for_question(self, question_id: str) -> list[Page]:
        """Return active TensionVerdict judgement pages for a question."""
        query = (
            self.client.table("pages")
            .select("*")
            .eq("page_type", PageType.JUDGEMENT.value)
            .eq("is_superseded", False)
            .eq("extra->tension_pair->>question_id", question_id)
            .not_.is_("extra->tension_verdict", "null")
        )
        if self._db.project_id:
            query = query.eq("project_id", self._db.project_id)
        query = self._db._staged_filter(query)
        rows = _rows(await self._db._execute(query.order("created_at", desc=True)))
        pages = [_row_to_page(r) for r in rows]
        pages = await self._db._apply_page_events(pages)
        return [p for p in pages if p.is_active()]

    async def get_dependents(
        self,
        page_id: str,
    ) -> list[tuple[Page, PageLink]]:
        """Return (dependent_page, link) for all pages that depend on this one."""
        links = await self.get_links_to(page_id)
        dep_links = [l for l in links if l.link_type == LinkType.DEPENDS_ON]
        if not dep_links:
            return []
        pages = await self._db.get_pages_by_ids([l.from_page_id for l in dep_links])
        return [
            (pages[l.from_page_id], l)
            for l in dep_links
            if l.from_page_id in pages and pages[l.from_page_id].is_active()
        ]

    async def get_dependencies(
        self,
        page_id: str,
    ) -> list[tuple[Page, PageLink]]:
        """Return (dependency_page, link) for all pages this one depends on."""
        links = await self.get_links_from(page_id)
        dep_links = [l for l in links if l.link_type == LinkType.DEPENDS_ON]
        if not dep_links:
            return []
        pages = await self._db.get_pages_by_ids([l.to_page_id for l in dep_links])
        return [(pages[l.to_page_id], l) for l in dep_links if l.to_page_id in pages]

    async def get_stale_dependencies(self) -> list[tuple[PageLink, int | None]]:
        """Return DEPENDS_ON links where the dependency has been superseded."""
        page_size = 1000
        offset = 0
        raw_rows: list[dict] = []
        while True:
            query = self.client.table("page_links").select("*").eq("link_type", "depends_on")
            query = self._db._staged_filter(query)
            rows = _rows(await self._db._execute(query.range(offset, offset + page_size - 1)))
            raw_rows.extend(rows)
            if len(rows) < page_size:
                break
            offset += page_size
        links = await self._db._apply_link_events([_row_to_link(r) for r in raw_rows])
        if not links:
            return []

        target_ids = list({l.to_page_id for l in links})
        pages_by_id = await self._db.get_pages_by_ids(target_ids)
        superseded_ids = [pid for pid, page in pages_by_id.items() if page.is_superseded]
        magnitudes = await self._get_supersession_magnitudes_many(superseded_ids)

        stale: list[tuple[PageLink, int | None]] = []
        for link in links:
            dep_page = pages_by_id.get(link.to_page_id)
            if dep_page and dep_page.is_superseded:
                stale.append((link, magnitudes.get(dep_page.id)))
        return stale

    async def get_dependency_counts(self) -> dict[str, int]:
        """Return a map from page_id to how many pages depend on it, within the current project."""
        project_page_ids: set[str] | None = None
        if self._db.project_id:
            project_page_ids = set()
            offset = 0
            page_size = 1000
            while True:
                pages_query = (
                    self.client.table("pages").select("id").eq("project_id", self._db.project_id)
                )
                pages_query = self._db._staged_filter(pages_query)
                rows = _rows(
                    await self._db._execute(pages_query.range(offset, offset + page_size - 1))
                )
                project_page_ids.update(r["id"] for r in rows)
                if len(rows) < page_size:
                    break
                offset += page_size

        page_size = 1000
        offset = 0
        raw_link_rows: list[dict] = []
        while True:
            query = (
                self.client.table("page_links")
                .select(_LINK_COLUMNS)
                .eq("link_type", LinkType.DEPENDS_ON.value)
            )
            query = self._db._staged_filter(query)
            rows = _rows(await self._db._execute(query.range(offset, offset + page_size - 1)))
            raw_link_rows.extend(rows)
            if len(rows) < page_size:
                break
            offset += page_size
        links = await self._db._apply_link_events([_row_to_link(r) for r in raw_link_rows])

        counts: dict[str, int] = {}
        for link in links:
            if project_page_ids is not None and (
                link.from_page_id not in project_page_ids or link.to_page_id not in project_page_ids
            ):
                continue
            counts[link.to_page_id] = counts.get(link.to_page_id, 0) + 1
        return counts

    async def _get_supersession_magnitude(self, page_id: str) -> int | None:
        """Look up the change_magnitude from the supersession mutation event."""
        result = await self._get_supersession_magnitudes_many([page_id])
        return result.get(page_id)

    async def _get_supersession_magnitudes_many(
        self,
        page_ids: Sequence[str],
    ) -> dict[str, int | None]:
        """Look up change_magnitude for many superseded pages in one query."""
        if not page_ids:
            return {}
        query = (
            self.client.table("mutation_events")
            .select("target_id, payload, created_at")
            .in_("target_id", list(set(page_ids)))
            .eq("event_type", "supersede_page")
            .order("created_at", desc=True)
        )
        rows = _rows(await self._db._execute(query))
        result: dict[str, int | None] = {}
        for row in rows:
            target = row["target_id"]
            if target in result:
                continue
            payload = row.get("payload") or {}
            result[target] = payload.get("change_magnitude")
        return result

    async def get_links_between(
        self,
        from_page_id: str,
        to_page_id: str,
    ) -> list[PageLink]:
        """Get all links from one page to another."""
        query = (
            self.client.table("page_links")
            .select("*")
            .eq("from_page_id", from_page_id)
            .eq("to_page_id", to_page_id)
        )
        query = self._db._staged_filter(query)
        rows = _rows(await self._db._execute(query))
        return await self._db._apply_link_events([_row_to_link(r) for r in rows])

    async def get_all_links(
        self,
        page_ids: set[str] | None = None,
    ) -> list[PageLink]:
        """Bulk-fetch links, scoped to a set of page IDs if provided."""
        if page_ids is not None:
            return await self._get_links_for_pages(page_ids)
        page_size = 2000
        if self._db.project_id:
            page_ids_query = self._db._staged_filter(
                self.client.table("pages").select("id").eq("project_id", self._db.project_id)
            )
            page_ids_rows = _rows(await self._db._execute(page_ids_query.limit(50000)))
            proj_page_ids = {r["id"] for r in page_ids_rows}
            all_rows: list[dict[str, Any]] = []
            offset = 0
            while True:
                query = self.client.table("page_links").select(_LINK_COLUMNS)
                query = self._db._staged_filter(query)
                rows = _rows(await self._db._execute(query.range(offset, offset + page_size - 1)))
                all_rows.extend(rows)
                if len(rows) < page_size:
                    break
                offset += page_size
            links = [
                _row_to_link(r)
                for r in all_rows
                if r["from_page_id"] in proj_page_ids or r["to_page_id"] in proj_page_ids
            ]
        else:
            all_rows = []
            offset = 0
            while True:
                query = self.client.table("page_links").select(_LINK_COLUMNS)
                query = self._db._staged_filter(query)
                rows = _rows(await self._db._execute(query.range(offset, offset + page_size - 1)))
                all_rows.extend(rows)
                if len(rows) < page_size:
                    break
                offset += page_size
            links = [_row_to_link(r) for r in all_rows]
        return await self._db._apply_link_events(links)

    async def _get_links_for_pages(
        self,
        page_ids: set[str],
    ) -> list[PageLink]:
        """Fetch links where at least one endpoint is in *page_ids*."""
        all_links: dict[str, PageLink] = {}
        id_list = list(page_ids)
        batch_size = 100
        page_size = 2000
        for start in range(0, len(id_list), batch_size):
            batch = id_list[start : start + batch_size]
            for col in ("from_page_id", "to_page_id"):
                offset = 0
                while True:
                    query = self.client.table("page_links").select(_LINK_COLUMNS).in_(col, batch)
                    query = self._db._staged_filter(query)
                    rows = _rows(
                        await self._db._execute(query.range(offset, offset + page_size - 1))
                    )
                    for r in rows:
                        link = _row_to_link(r)
                        all_links[link.id] = link
                    if len(rows) < page_size:
                        break
                    offset += page_size
        return await self._db._apply_link_events(list(all_links.values()))

    async def delete_link(self, link_id: str) -> None:
        """Delete a page link by ID. Records a mutation event first."""
        rows = _rows(
            await self._db._execute(
                self._db._staged_filter(
                    self.client.table("page_links").select("*").eq("id", link_id)
                )
            )
        )
        link_snapshot = rows[0] if rows else {}
        await self._db.record_mutation_event("delete_link", link_id, link_snapshot)
        if not self._db.staged:
            await self._db._execute(self.client.table("page_links").delete().eq("id", link_id))

    async def update_link_role(self, link_id: str, role: LinkRole) -> None:
        """Update a link's role. Records a mutation event first."""
        link = await self.get_link(link_id)
        old_role = link.role.value if link else None
        await self._db.record_mutation_event(
            "change_link_role",
            link_id,
            {"new_role": role.value, "old_role": old_role},
        )
        if not self._db.staged:
            await self._db._execute(
                self.client.table("page_links").update({"role": role.value}).eq("id", link_id)
            )
