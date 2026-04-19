"""PageStore: everything keyed on a single ``pages`` row.

Owns page CRUD (``save_page``, ``update_page_*``, ``supersede_page``),
read helpers (``get_page``, ``get_pages_by_ids``, ``get_pages``,
``get_pages_paginated``, ``get_pages_slim``), ID/short-ID resolution,
supersession chain walking, and a handful of project-scoped page
aggregates (``get_root_questions``, ``get_human_questions``,
``count_pages_for_question``, ``get_assess_staleness``,
``count_pages_since``, ``workspace_coverage``).

**Critical invariant**: ``update_page_content`` and ``supersede_page``
are mutation-tracked writes. They record a ``mutation_events`` row
via ``self._db.record_mutation_event`` first, then dual-write to the
base table only when the run is not staged. Do not add other writes
to ``pages`` here without following the same pattern (or documenting
explicitly why the field is not part of the staged-runs model — see
``merge_page_extra`` for the exception).

``_staged_filter`` + ``_apply_page_events`` still live on ``DB`` as
shared machinery — this store consumes them through ``self._db``.
Hardening them into a capability object is a later phase.
"""

import logging
from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from postgrest.types import CountMethod

from rumil.db.row_helpers import (
    _LINK_COLUMNS,
    _SLIM_PAGE_COLUMNS,
    _row_to_link,
    _row_to_page,
    _rows,
)
from rumil.models import CallStatus, CallType, Page, PageType, Workspace

if TYPE_CHECKING:
    from rumil.database import DB


log = logging.getLogger(__name__)


class PageStore:
    """Reads and mutation-logged writes on the ``pages`` table."""

    def __init__(self, db: "DB") -> None:
        self._db = db

    @property
    def client(self) -> Any:
        return self._db.client

    async def save_page(self, page: Page) -> None:
        if not page.project_id:
            page.project_id = self._db.project_id
        await self._db._execute(
            self.client.table("pages").upsert(
                {
                    "id": page.id,
                    "page_type": page.page_type.value,
                    "layer": page.layer.value,
                    "workspace": page.workspace.value,
                    "content": page.content,
                    "headline": page.headline,
                    "project_id": page.project_id,
                    "epistemic_status": page.epistemic_status,
                    "epistemic_type": page.epistemic_type,
                    "credence": page.credence,
                    "credence_reasoning": page.credence_reasoning,
                    "robustness": page.robustness,
                    "robustness_reasoning": page.robustness_reasoning,
                    "provenance_model": page.provenance_model,
                    "provenance_call_type": page.provenance_call_type,
                    "provenance_call_id": page.provenance_call_id,
                    "created_at": page.created_at.isoformat(),
                    "superseded_by": page.superseded_by,
                    "is_superseded": page.is_superseded,
                    "extra": page.extra,
                    "importance": page.importance,
                    "fruit_remaining": page.fruit_remaining,
                    "sections": page.sections,
                    "meta_type": page.meta_type,
                    "run_id": self._db.run_id,
                    "staged": self._db.staged,
                    "abstract": page.abstract,
                    "task_shape": page.task_shape,
                }
            )
        )

    async def update_page_importance(self, page_id: str, importance: int) -> None:
        """Update the importance level on a page."""
        await self._db._execute(
            self.client.table("pages").update({"importance": importance}).eq("id", page_id)
        )

    async def update_page_content(self, page_id: str, new_content: str) -> None:
        """Update a page's content field with mutation event recording."""
        page = await self.get_page(page_id)
        if not page:
            raise ValueError(f"update_page_content: page {page_id} not found")
        await self._db.record_mutation_event(
            "update_page_content",
            page_id,
            {"old_content": page.content, "new_content": new_content},
        )
        if not self._db.staged:
            await self._db._execute(
                self.client.table("pages").update({"content": new_content}).eq("id", page_id)
            )

    async def update_page_abstract(self, page_id: str, abstract: str) -> None:
        await self._db._execute(
            self.client.table("pages").update({"abstract": abstract}).eq("id", page_id)
        )

    async def update_page_task_shape(self, page_id: str, task_shape: dict | None) -> None:
        """Set the task_shape JSONB payload on a page.

        Task-shape is metadata attached only to questions (v1 taxonomy).
        Non-question pages always store NULL.
        """
        await self._db._execute(
            self.client.table("pages").update({"task_shape": task_shape}).eq("id", page_id)
        )

    async def workspace_coverage(self) -> dict[str, dict[str, int]]:
        """Aggregate task_shape tag values across all question pages in the project."""
        query = (
            self.client.table("pages")
            .select("task_shape")
            .eq("page_type", PageType.QUESTION.value)
            .not_.is_("task_shape", "null")
        )
        if self._db.project_id:
            query = query.eq("project_id", self._db.project_id)
        query = self._db._staged_filter(query)
        rows = _rows(await self._db._execute(query))
        coverage: dict[str, dict[str, int]] = {}
        for row in rows:
            shape = row.get("task_shape") or {}
            if not isinstance(shape, dict):
                continue
            for dim, value in shape.items():
                if not isinstance(value, str):
                    continue
                coverage.setdefault(dim, {})
                coverage[dim][value] = coverage[dim].get(value, 0) + 1
        return coverage

    async def merge_page_extra(self, page_id: str, updates: dict) -> None:
        """Merge ``updates`` into a page's ``extra`` JSONB column.

        No mutation event is recorded — ``extra`` is append-only metadata
        that is not part of the staged-runs mutation surface.
        """
        page = await self.get_page(page_id)
        if not page:
            raise ValueError(f"merge_page_extra: page {page_id} not found")
        merged = {**(page.extra or {}), **updates}
        await self._db._execute(
            self.client.table("pages").update({"extra": merged}).eq("id", page_id)
        )

    async def get_page(self, page_id: str) -> Page | None:
        query = self.client.table("pages").select("*").eq("id", page_id)
        return await self._db.overlay.read_page_opt(query)

    async def get_pages_by_ids(self, page_ids: Sequence[str]) -> dict[str, Page]:
        """Bulk-fetch pages by ID. Returns {id: Page} for pages that exist."""
        if not page_ids:
            return {}
        result: dict[str, Page] = {}
        id_list = list(page_ids)
        batch_size = 200
        for start in range(0, len(id_list), batch_size):
            batch = id_list[start : start + batch_size]
            query = self.client.table("pages").select("*").in_("id", batch)
            for page in await self._db.overlay.read_pages(query):
                result[page.id] = page
        return result

    async def resolve_page_ids(self, page_ids: Sequence[str]) -> dict[str, str]:
        """Batch-resolve a mix of full UUIDs and 8-char short IDs."""
        if not page_ids:
            return {}
        cleaned: list[str] = [pid.strip() for pid in page_ids if pid and pid.strip()]
        full_ids = [pid for pid in cleaned if len(pid) > 8]
        short_ids = [pid for pid in cleaned if len(pid) <= 8]

        resolved: dict[str, str] = {}

        if full_ids:
            rows = _rows(
                await self._db._execute(
                    self.client.table("pages").select("id").in_("id", list(set(full_ids)))
                )
            )
            existing = {r["id"] for r in rows}
            for pid in full_ids:
                if pid in existing:
                    resolved[pid] = pid

        if short_ids:
            unique_short = list({pid for pid in short_ids})
            or_clause = ",".join(f"id.like.{p}%" for p in unique_short)
            rows = _rows(
                await self._db._execute(self.client.table("pages").select("id").or_(or_clause))
            )
            matches_by_prefix: dict[str, list[str]] = {p: [] for p in unique_short}
            for r in rows:
                full = r["id"]
                for p in unique_short:
                    if full.startswith(p):
                        matches_by_prefix[p].append(full)
            for pid in short_ids:
                hits = matches_by_prefix.get(pid, [])
                if len(hits) == 1:
                    resolved[pid] = hits[0]
                elif len(hits) > 1:
                    log.warning("Ambiguous short ID '%s' matches %d pages", pid, len(hits))
        return resolved

    async def resolve_page_id(self, page_id: str) -> str | None:
        """Resolve a page ID to a full UUID. Handles full UUIDs + 8-char short IDs + URLs."""
        if not page_id:
            return None
        rows = _rows(
            await self._db._execute(self.client.table("pages").select("id").eq("id", page_id))
        )
        if rows:
            return rows[0]["id"]
        if len(page_id) <= 8:
            rows = _rows(
                await self._db._execute(
                    self.client.table("pages").select("id").like("id", f"{page_id}%")
                )
            )
            if len(rows) == 1:
                return rows[0]["id"]
            if len(rows) > 1:
                log.warning(
                    "Ambiguous short ID '%s' matches %d pages",
                    page_id,
                    len(rows),
                )
            return None
        if page_id.startswith("http"):
            rows = _rows(
                await self._db._execute(
                    self.client.table("pages").select("id").eq("extra->>url", page_id)
                )
            )
            if rows:
                return rows[0]["id"]
        return None

    async def resolve_link_id(self, link_id: str) -> str | None:
        """Resolve a link ID to a full UUID. Handles both full UUIDs and
        8-char short IDs. Returns the full UUID if found, or None.

        Lives on PageStore because it's read-only and pairs naturally
        with resolve_page_id / resolve_call_id — a future LinkStore
        extraction may move it.
        """
        if not link_id:
            return None
        rows = _rows(
            await self._db._execute(self.client.table("page_links").select("id").eq("id", link_id))
        )
        if rows:
            return rows[0]["id"]
        if len(link_id) <= 8:
            rows = _rows(
                await self._db._execute(
                    self.client.table("page_links").select("id").like("id", f"{link_id}%")
                )
            )
            if len(rows) == 1:
                return rows[0]["id"]
            if len(rows) > 1:
                log.warning(
                    "Ambiguous short ID '%s' matches %d links",
                    link_id,
                    len(rows),
                )
        return None

    async def page_label(self, page_id: str) -> str:
        """Return a human-readable label like '"Summary text" [short_id]'."""
        page = await self.get_page(page_id)
        if page:
            return f'"{page.headline[:60]}" [{page_id[:8]}]'
        return f"[{page_id[:8]}]"

    async def get_pages_slim(self, active_only: bool = True) -> list[Page]:
        """Fetch all pages without the content field — safe for bulk loads."""
        query = self.client.table("pages").select(_SLIM_PAGE_COLUMNS)
        if self._db.project_id:
            query = query.eq("project_id", self._db.project_id)
        if active_only:
            query = query.eq("is_superseded", False)
        query = self._db._staged_filter(query)
        pages = [
            _row_to_page(r)
            for r in _rows(
                await self._db._execute(query.order("created_at", desc=True).limit(10000))
            )
        ]
        pages = await self._db._apply_page_events(pages)
        if active_only:
            pages = [p for p in pages if p.is_active()]
        return pages

    async def get_pages(
        self,
        workspace: Workspace | None = None,
        page_type: PageType | None = None,
        active_only: bool = True,
    ) -> list[Page]:
        query = self.client.table("pages").select("*")
        if self._db.project_id:
            query = query.eq("project_id", self._db.project_id)
        if workspace:
            query = query.eq("workspace", workspace.value)
        if page_type:
            query = query.eq("page_type", page_type.value)
        if active_only:
            query = query.eq("is_superseded", False)
        query = self._db._staged_filter(query)
        pages = [
            _row_to_page(r)
            for r in _rows(
                await self._db._execute(query.order("created_at", desc=True).limit(10000))
            )
        ]
        pages = await self._db._apply_page_events(pages)
        if active_only:
            pages = [p for p in pages if p.is_active()]
        return pages

    async def supersede_page(
        self,
        old_id: str,
        new_id: str,
        change_magnitude: int | None = None,
    ) -> None:
        payload: dict = {"new_page_id": new_id}
        if change_magnitude is not None:
            payload["change_magnitude"] = change_magnitude

        await self._db.record_mutation_event(
            "supersede_page",
            old_id,
            payload,
        )

        if not self._db.staged:
            await self._db._execute(
                self.client.table("pages")
                .update(
                    {
                        "is_superseded": True,
                        "superseded_by": new_id,
                    }
                )
                .eq("id", old_id)
            )

    async def get_pages_paginated(
        self,
        workspace: Workspace | None = None,
        page_type: PageType | None = None,
        active_only: bool = True,
        search: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[Sequence[Page], int]:
        """Return a page of results and the total matching count."""
        query = self.client.table("pages").select("*", count=CountMethod.exact)
        if self._db.project_id:
            query = query.eq("project_id", self._db.project_id)
        if workspace:
            query = query.eq("workspace", workspace.value)
        if page_type:
            query = query.eq("page_type", page_type.value)
        if active_only:
            query = query.eq("is_superseded", False)
        if search:
            query = query.or_(f"headline.ilike.%{search}%,content.ilike.%{search}%")
        query = self._db._staged_filter(query)
        query = query.order("is_human_created", desc=True).order("created_at", desc=True)
        end = offset + limit - 1
        result = await self._db._execute(query.range(offset, end))
        total = result.count or 0
        pages = [_row_to_page(r) for r in _rows(result)]
        pages = await self._db._apply_page_events(pages)
        if active_only:
            pages = [p for p in pages if p.is_active()]
        return pages, total

    async def resolve_supersession_chain(
        self,
        page_id: str,
        max_depth: int = 10,
    ) -> Page | None:
        """Follow superseded_by links from *page_id* to the final active page."""
        results = await self.resolve_supersession_chains(
            [page_id],
            max_depth=max(0, max_depth - 1),
        )
        return results.get(page_id)

    async def resolve_supersession_chains(
        self,
        page_ids: Sequence[str],
        max_depth: int = 10,
    ) -> dict[str, Page]:
        """Bulk-resolve supersession chains for multiple page IDs."""
        pages = await self.get_pages_by_ids(list(page_ids))
        pending: dict[str, str] = {}
        result: dict[str, Page] = {}

        for pid in page_ids:
            page = pages.get(pid)
            if not page or not page.is_superseded or not page.superseded_by:
                continue
            pending[pid] = page.superseded_by

        for _ in range(max_depth):
            if not pending:
                break
            targets = list(set(pending.values()))
            fetched = await self.get_pages_by_ids(targets)
            next_pending: dict[str, str] = {}
            for orig_id, target_id in pending.items():
                target_page = fetched.get(target_id)
                if not target_page:
                    continue
                if not target_page.is_superseded:
                    result[orig_id] = target_page
                elif target_page.superseded_by:
                    next_pending[orig_id] = target_page.superseded_by
            pending = next_pending

        return result

    async def get_root_questions(
        self,
        workspace: Workspace = Workspace.RESEARCH,
        *,
        include_duplicates: bool = False,
    ) -> list[Page]:
        """Return questions that have no parent (top-level questions)."""
        params: dict[str, Any] = {"ws": workspace.value}
        if self._db.project_id:
            params["pid"] = self._db.project_id
        if self._db.staged:
            params["p_staged_run_id"] = self._db.run_id
        if self._db.snapshot_ts is not None:
            params["p_snapshot_ts"] = self._db.snapshot_ts.isoformat()
        if include_duplicates:
            params["p_include_duplicates"] = True
        rows = _rows(await self._db._execute(self.client.rpc("get_root_questions", params)))
        pages = [_row_to_page(r) for r in rows]
        return await self._db._apply_page_events(pages)

    async def get_human_questions(
        self,
        workspace: Workspace = Workspace.RESEARCH,
    ) -> list[Page]:
        """Return all active, human-authored questions in *workspace*."""
        query = (
            self.client.table("pages")
            .select("*")
            .eq("page_type", PageType.QUESTION.value)
            .eq("workspace", workspace.value)
            .eq("is_human_created", True)
            .eq("is_superseded", False)
        )
        if self._db.project_id:
            query = query.eq("project_id", self._db.project_id)
        query = self._db._staged_filter(query)
        rows = _rows(await self._db._execute(query))
        pages = [_row_to_page(r) for r in rows]
        pages = await self._db._apply_page_events(pages)
        return [p for p in pages if p.is_active()]

    async def count_pages_for_question(self, question_id: str) -> dict:
        """Count pages linked to or created in context of a question."""
        cons_result = await self._db._execute(
            self.client.table("page_links")
            .select("id", count=CountMethod.exact)
            .eq("to_page_id", question_id)
            .eq("link_type", "consideration")
        )
        judgements_result = await self._db._execute(
            self.client.rpc(
                "count_active_judgements",
                {"qid": question_id},
            )
        )
        return {
            "considerations": cons_result.count or 0,
            "judgements": cast(int, judgements_result.data or 0),
        }

    async def get_assess_staleness(
        self,
        question_ids: Sequence[str],
    ) -> dict[str, bool]:
        """Check whether questions need re-assessment."""
        if not question_ids:
            return {}

        calls_query = (
            self.client.table("calls")
            .select("scope_page_id,created_at")
            .eq("call_type", CallType.ASSESS.value)
            .eq("status", CallStatus.COMPLETE.value)
            .in_("scope_page_id", list(question_ids))
            .order("created_at", desc=True)
        )
        calls_result = await self._db._execute(calls_query)

        latest_assess: dict[str, datetime] = {}
        for row in _rows(calls_result):
            qid = row["scope_page_id"]
            if qid not in latest_assess:
                latest_assess[qid] = datetime.fromisoformat(row["created_at"])

        links_query = (
            self.client.table("page_links")
            .select(_LINK_COLUMNS)
            .in_("to_page_id", list(question_ids))
        )
        links_query = self._db._staged_filter(links_query)
        links_result = await self._db._execute(links_query)
        links = [_row_to_link(r) for r in _rows(links_result)]
        links = await self._db._apply_link_events(links)

        latest_link: dict[str, datetime] = {}
        for link in links:
            qid = link.to_page_id
            ts = link.created_at
            if qid not in latest_link or ts > latest_link[qid]:
                latest_link[qid] = ts

        staleness: dict[str, bool] = {}
        for qid in question_ids:
            if qid not in latest_assess or (
                qid in latest_link and latest_link[qid] > latest_assess[qid]
            ):
                staleness[qid] = True
            else:
                staleness[qid] = False
        return staleness

    async def count_pages_since(self, since: datetime) -> int:
        """Count workspace pages created after *since* (for cache invalidation)."""
        query = (
            self.client.table("pages")
            .select("id", count=CountMethod.exact)
            .gt("created_at", since.isoformat())
        )
        if self._db.project_id:
            query = query.eq("project_id", self._db.project_id)
        query = self._db._staged_filter(query)
        result = await self._db._execute(query)
        return result.count or 0
