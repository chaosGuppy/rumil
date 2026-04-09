"""
Supabase database layer for the research workspace.
"""

import asyncio
import logging
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any, cast

import httpx
from postgrest.types import CountMethod
from supabase import acreate_client, AsyncClient
from supabase.lib.client_options import AsyncClientOptions
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from rumil.settings import get_settings
from rumil.models import (
    Call,
    CallSequence,
    CallStatus,
    CallType,
    ConsiderationDirection,
    LinkRole,
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Project,
    Workspace,
)

# Supabase SDK types APIResponse.data as JSON | None, but table queries
# always return list[dict]. We cast to this alias for clarity.
log = logging.getLogger(__name__)

_Rows = list[dict[str, Any]]


def _rows(response: Any) -> _Rows:
    """Extract rows from a Supabase API response with proper typing."""
    return cast(_Rows, response.data) if response.data else []


_DB_RETRYABLE_EXCEPTIONS = (
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.ConnectError,
    httpx.ReadError,
    httpx.RemoteProtocolError,
)


def _stop_after_db_retries(retry_state: RetryCallState) -> bool:
    return retry_state.attempt_number >= get_settings().max_db_retries


def _log_db_retry(retry_state: RetryCallState) -> None:
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    wait = retry_state.next_action.sleep if retry_state.next_action else 0
    max_retries = get_settings().max_db_retries
    log.warning(
        "DB request failed (%s), retrying in %gs (attempt %d/%d)",
        type(exc).__name__ if exc else "unknown",
        wait,
        retry_state.attempt_number,
        max_retries,
    )


_db_retry = retry(
    retry=retry_if_exception_type(_DB_RETRYABLE_EXCEPTIONS),
    stop=_stop_after_db_retries,
    wait=wait_exponential(multiplier=0.5, min=0.5, max=60),
    before_sleep=_log_db_retry,
    reraise=True,
)


_SLIM_PAGE_COLUMNS = (
    'id,page_type,layer,workspace,headline,abstract,'
    'epistemic_status,epistemic_type,credence,robustness,extra,is_superseded,'
    'project_id,created_at,superseded_by,run_id'
)


def _row_to_page(row: dict[str, Any]) -> Page:
    return Page(
        id=row["id"],
        page_type=PageType(row["page_type"]),
        layer=PageLayer(row["layer"]),
        workspace=Workspace(row["workspace"]),
        content=row.get("content") or "",
        headline=row["headline"],
        project_id=row.get("project_id") or "",
        epistemic_status=row.get("epistemic_status") or 0.0,
        epistemic_type=row.get("epistemic_type") or "",
        credence=row.get("credence"),
        robustness=row.get("robustness"),
        provenance_model=row.get("provenance_model") or "",
        provenance_call_type=row.get("provenance_call_type") or "",
        provenance_call_id=row.get("provenance_call_id") or "",
        created_at=datetime.fromisoformat(row["created_at"]),
        superseded_by=row.get("superseded_by"),
        is_superseded=bool(row.get("is_superseded", False)),
        extra=row.get("extra") or {},
        abstract=row.get("abstract") or "",
        fruit_remaining=row.get("fruit_remaining"),
    )


def _row_to_link(row: dict[str, Any]) -> PageLink:
    return PageLink(
        id=row["id"],
        from_page_id=row["from_page_id"],
        to_page_id=row["to_page_id"],
        link_type=LinkType(row["link_type"]),
        direction=(
            ConsiderationDirection(row["direction"]) if row["direction"] else None
        ),
        strength=row["strength"],
        reasoning=row["reasoning"] or "",
        role=LinkRole(row.get("role", "direct")),
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _row_to_call(row: dict[str, Any]) -> Call:
    return Call(
        id=row["id"],
        call_type=CallType(row["call_type"]),
        workspace=Workspace(row["workspace"]),
        project_id=row.get("project_id") or "",
        status=CallStatus(row["status"]),
        parent_call_id=row["parent_call_id"],
        scope_page_id=row["scope_page_id"],
        budget_allocated=row["budget_allocated"],
        budget_used=row["budget_used"],
        context_page_ids=row.get("context_page_ids") or [],
        result_summary=row.get("result_summary") or "",
        review_json=row.get("review_json") or {},
        call_params=row.get("call_params"),
        created_at=datetime.fromisoformat(row["created_at"]),
        completed_at=(
            datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None
        ),
        sequence_id=row.get("sequence_id"),
        sequence_position=row.get("sequence_position"),
        cost_usd=row.get("cost_usd"),
    )


def _row_to_call_sequence(row: dict[str, Any]) -> CallSequence:
    return CallSequence(
        id=row["id"],
        parent_call_id=row.get("parent_call_id"),
        run_id=row.get("run_id", ""),
        scope_question_id=row.get("scope_question_id"),
        position_in_batch=row.get("position_in_batch", 0),
        created_at=datetime.fromisoformat(row["created_at"]),
    )


class MutationState:
    """Cached mutation events for a staged run, keyed by target_id."""

    __slots__ = ("superseded_pages", "deleted_links", "link_role_overrides")

    def __init__(self) -> None:
        self.superseded_pages: dict[str, str] = {}
        self.deleted_links: set[str] = set()
        self.link_role_overrides: dict[str, LinkRole] = {}


class DB:
    def __init__(
        self,
        run_id: str,
        client: AsyncClient,
        project_id: str = "",
        staged: bool = False,
        ab_run_id: str | None = None,
    ):
        self.run_id = run_id
        self.client = client
        self.project_id = project_id
        self.staged = staged
        self.ab_run_id = ab_run_id
        self._semaphore = asyncio.Semaphore(
            get_settings().db_max_concurrent_queries
        )
        self._prod: bool = False
        self._mutation_cache: MutationState | None = None

    @classmethod
    async def create(
        cls,
        run_id: str,
        prod: bool = False,
        project_id: str = "",
        client: AsyncClient | None = None,
        staged: bool = False,
        ab_run_id: str | None = None,
    ) -> "DB":
        if client is None:
            url, key = get_settings().get_supabase_credentials(prod)
            client = await acreate_client(
                url, key, options=AsyncClientOptions(schema="public")
            )
        db = cls(
            run_id=run_id, client=client, project_id=project_id,
            staged=staged, ab_run_id=ab_run_id,
        )
        db._prod = prod
        return db

    async def fork(self) -> "DB":
        """Create a new DB instance with a fresh Supabase client.

        Shares run_id, project_id, and staged flag with the parent but gets
        its own HTTP connection. Use this to scope connections to a single
        call, avoiding HTTP/2 stream exhaustion on long-running jobs.
        """
        url, key = get_settings().get_supabase_credentials(self._prod)
        client = await acreate_client(
            url, key, options=AsyncClientOptions(schema="public")
        )
        db = DB(
            run_id=self.run_id,
            client=client,
            project_id=self.project_id,
            staged=self.staged,
            ab_run_id=self.ab_run_id,
        )
        db._prod = self._prod
        return db

    async def close(self) -> None:
        """Close the underlying HTTP connections."""
        try:
            await self.client.postgrest.aclose()
        except Exception:
            log.debug('Failed to close postgrest client', exc_info=True)

    @_db_retry
    async def _execute(self, query: Any) -> Any:
        """Execute a query builder through the concurrency semaphore."""
        async with self._semaphore:
            return await query.execute()

    def _staged_filter(self, query: Any) -> Any:
        """Apply staged-run visibility filter to a query.

        Staged runs see baseline (staged=false) + their own rows.
        Non-staged runs see only baseline rows.
        """
        if self.staged:
            return query.or_(f"staged.eq.false,run_id.eq.{self.run_id}")
        return query.eq("staged", False)

    async def _load_mutation_state(self) -> MutationState:
        """Fetch and cache mutation events for this staged run."""
        if self._mutation_cache is not None:
            return self._mutation_cache
        if not self.staged:
            self._mutation_cache = MutationState()
            return self._mutation_cache
        rows = _rows(
            await self._execute(
                self.client.table("mutation_events")
                .select("event_type, target_id, payload")
                .eq("run_id", self.run_id)
                .order("created_at")
            )
        )
        state = MutationState()
        for row in rows:
            et = row["event_type"]
            tid = row["target_id"]
            payload = row.get("payload") or {}
            if et == "supersede_page":
                state.superseded_pages[tid] = payload.get("new_page_id", "")
            elif et == "delete_link":
                state.deleted_links.add(tid)
            elif et == "change_link_role":
                state.link_role_overrides[tid] = LinkRole(payload["new_role"])
        self._mutation_cache = state
        return state

    def _invalidate_mutation_cache(self) -> None:
        self._mutation_cache = None

    async def _apply_page_events(self, pages: Sequence[Page]) -> list[Page]:
        """Overlay mutation events onto a batch of pages."""
        state = await self._load_mutation_state()
        if not state.superseded_pages:
            return list(pages)
        result: list[Page] = []
        for p in pages:
            if p.id in state.superseded_pages:
                p = p.model_copy(update={
                    "is_superseded": True,
                    "superseded_by": state.superseded_pages[p.id],
                })
            result.append(p)
        return result

    async def _apply_link_events(self, links: Sequence[PageLink]) -> list[PageLink]:
        """Overlay mutation events onto a batch of links."""
        state = await self._load_mutation_state()
        if not state.deleted_links and not state.link_role_overrides:
            return list(links)
        result: list[PageLink] = []
        for link in links:
            if link.id in state.deleted_links:
                continue
            if link.id in state.link_role_overrides:
                link = link.model_copy(update={
                    "role": state.link_role_overrides[link.id],
                })
            result.append(link)
        return result

    async def record_mutation_event(
        self, event_type: str, target_id: str, payload: dict,
    ) -> None:
        """Record a mutation event for undo/staging support."""
        await self._execute(
            self.client.table("mutation_events").insert({
                "id": str(uuid.uuid4()),
                "run_id": self.run_id,
                "event_type": event_type,
                "target_id": target_id,
                "payload": payload,
            })
        )
        self._invalidate_mutation_cache()

    async def get_or_create_project(self, name: str) -> Project:
        rows = _rows(
            await self._execute(
                self.client.table("projects").select("*").eq("name", name)
            )
        )
        if rows:
            row = rows[0]
            return Project(
                id=row["id"],
                name=row["name"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
        row = _rows(
            await self._execute(
                self.client.table("projects").insert({"name": name})
            )
        )[0]
        return Project(
            id=row["id"],
            name=row["name"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    async def list_projects(self) -> list[Project]:
        rows = _rows(
            await self._execute(
                self.client.table("projects")
                .select("*")
                .order("created_at")
            )
        )
        return [
            Project(
                id=r["id"],
                name=r["name"],
                created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in rows
        ]

    # --- Pages ---

    async def save_page(self, page: Page) -> None:
        log.debug(
            "save_page: id=%s, type=%s, headline=%s",
            page.id[:8], page.page_type.value, page.headline[:60],
        )
        if not page.project_id:
            page.project_id = self.project_id
        await self._execute(
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
                    "robustness": page.robustness,
                    "provenance_model": page.provenance_model,
                    "provenance_call_type": page.provenance_call_type,
                    "provenance_call_id": page.provenance_call_id,
                    "created_at": page.created_at.isoformat(),
                    "superseded_by": page.superseded_by,
                    "is_superseded": page.is_superseded,
                    "extra": page.extra,
                    "fruit_remaining": page.fruit_remaining,
                    "run_id": self.run_id,
                    "staged": self.staged,
                    "abstract": page.abstract,
                }
            )
        )

    async def update_page_extra(self, page_id: str, extra: dict) -> None:
        """Update the extra JSONB field on a page in place."""
        await self._execute(
            self.client.table("pages").update(
                {"extra": extra}
            ).eq("id", page_id)
        )

    async def get_concept_registry(self) -> list[Page]:
        """Return all concept proposals in the concept_staging workspace."""
        return await self.get_pages(
            workspace=Workspace.CONCEPT_STAGING,
            page_type=PageType.CONCEPT,
            active_only=False,
        )

    async def update_page_abstract(
        self, page_id: str, abstract: str
    ) -> None:
        await self._execute(
            self.client.table("pages").update(
                {"abstract": abstract}
            ).eq("id", page_id)
        )

    async def get_page(self, page_id: str) -> Page | None:
        query = self.client.table("pages").select("*").eq("id", page_id)
        query = self._staged_filter(query)
        rows = _rows(await self._execute(query))
        if not rows:
            return None
        pages = await self._apply_page_events([_row_to_page(rows[0])])
        if not pages:
            return None
        await self.apply_epistemic_overrides(pages)
        return pages[0]

    async def get_pages_by_ids(self, page_ids: Sequence[str]) -> dict[str, Page]:
        """Bulk-fetch pages by ID. Returns {id: Page} for pages that exist."""
        if not page_ids:
            return {}
        result: dict[str, Page] = {}
        id_list = list(page_ids)
        batch_size = 200
        for start in range(0, len(id_list), batch_size):
            batch = id_list[start:start + batch_size]
            rows = _rows(
                await self._execute(
                    self._staged_filter(
                        self.client.table("pages").select("*").in_("id", batch)
                    )
                )
            )
            for r in rows:
                page = _row_to_page(r)
                result[page.id] = page
        pages = await self._apply_page_events(list(result.values()))
        await self.apply_epistemic_overrides(pages)
        return {p.id: p for p in pages}

    async def resolve_page_ids(
        self, page_ids: Sequence[str]
    ) -> dict[str, str]:
        """Batch-resolve a mix of full UUIDs and 8-char short IDs.

        Returns a mapping from each input id to its resolved full UUID,
        omitting inputs that can't be resolved (not found, or ambiguous
        short prefix). At most two queries are issued regardless of
        input size: one for full-id matches, one for short-id prefix
        matches.
        """
        if not page_ids:
            return {}
        cleaned: list[str] = [pid.strip() for pid in page_ids if pid and pid.strip()]
        full_ids = [pid for pid in cleaned if len(pid) > 8]
        short_ids = [pid for pid in cleaned if len(pid) <= 8]

        resolved: dict[str, str] = {}

        if full_ids:
            rows = _rows(
                await self._execute(
                    self.client.table("pages")
                    .select("id")
                    .in_("id", list(set(full_ids)))
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
                await self._execute(
                    self.client.table("pages")
                    .select("id")
                    .or_(or_clause)
                )
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
                    log.warning(
                        "Ambiguous short ID '%s' matches %d pages", pid, len(hits)
                    )
        return resolved

    async def resolve_page_id(self, page_id: str) -> str | None:
        """Resolve a page ID to a full UUID. Handles both full UUIDs and
        8-char short IDs. Returns the full UUID if found, or None."""
        if not page_id:
            log.debug("resolve_page_id: empty page_id")
            return None
        # Try exact match first
        rows = _rows(
            await self._execute(
                self.client.table("pages").select("id").eq("id", page_id)
            )
        )
        if rows:
            log.debug("resolve_page_id: exact match for %s", page_id[:8])
            return rows[0]["id"]
        # Try prefix match for short IDs
        if len(page_id) <= 8:
            rows = _rows(
                await self._execute(
                    self.client.table("pages")
                    .select("id")
                    .like("id", f"{page_id}%")
                )
            )
            if len(rows) == 1:
                log.debug(
                    "resolve_page_id: prefix match %s -> %s",
                    page_id, rows[0]["id"][:8],
                )
                return rows[0]["id"]
            if len(rows) > 1:
                log.warning(
                    "Ambiguous short ID '%s' matches %d pages", page_id, len(rows),
                )
            else:
                log.debug("resolve_page_id: no prefix match for %s", page_id)
            return None
        if page_id.startswith("http"):
            rows = _rows(
                await self._execute(
                    self.client.table("pages")
                    .select("id")
                    .eq("extra->>url", page_id)
                )
            )
            if len(rows) == 1:
                log.debug(
                    "resolve_page_id: URL match %s -> %s",
                    page_id, rows[0]["id"][:8],
                )
                return rows[0]["id"]
            if len(rows) > 1:
                log.debug(
                    "resolve_page_id: URL match %s -> %s (first of %d)",
                    page_id, rows[0]["id"][:8], len(rows),
                )
                return rows[0]["id"]
        log.debug("resolve_page_id: no match for %s", page_id[:8])
        return None

    async def resolve_call_id(self, call_id: str) -> str | None:
        """Resolve a call ID to a full UUID. Handles both full UUIDs and
        8-char short IDs. Returns the full UUID if found, or None."""
        if not call_id:
            return None
        rows = _rows(
            await self._execute(
                self.client.table("calls").select("id").eq("id", call_id)
            )
        )
        if rows:
            return rows[0]["id"]
        if len(call_id) <= 8:
            rows = _rows(
                await self._execute(
                    self.client.table("calls")
                    .select("id")
                    .like("id", f"{call_id}%")
                )
            )
            if len(rows) == 1:
                return rows[0]["id"]
            if len(rows) > 1:
                log.warning(
                    "Ambiguous short ID '%s' matches %d calls",
                    call_id,
                    len(rows),
                )
        return None

    async def resolve_link_id(self, link_id: str) -> str | None:
        """Resolve a link ID to a full UUID. Handles both full UUIDs and
        8-char short IDs. Returns the full UUID if found, or None."""
        if not link_id:
            return None
        rows = _rows(
            await self._execute(
                self.client.table("page_links").select("id").eq("id", link_id)
            )
        )
        if rows:
            return rows[0]["id"]
        if len(link_id) <= 8:
            rows = _rows(
                await self._execute(
                    self.client.table("page_links")
                    .select("id")
                    .like("id", f"{link_id}%")
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
        if self.project_id:
            query = query.eq("project_id", self.project_id)
        if active_only:
            query = query.eq("is_superseded", False)
        query = self._staged_filter(query)
        pages = [
            _row_to_page(r)
            for r in _rows(
                await self._execute(
                    query.order("created_at", desc=True).limit(10000)
                )
            )
        ]
        pages = await self._apply_page_events(pages)
        await self.apply_epistemic_overrides(pages)
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
        if self.project_id:
            query = query.eq("project_id", self.project_id)
        if workspace:
            query = query.eq("workspace", workspace.value)
        if page_type:
            query = query.eq("page_type", page_type.value)
        if active_only:
            query = query.eq("is_superseded", False)
        query = self._staged_filter(query)
        pages = [
            _row_to_page(r)
            for r in _rows(
                await self._execute(
                    query.order("created_at", desc=True).limit(10000)
                )
            )
        ]
        pages = await self._apply_page_events(pages)
        await self.apply_epistemic_overrides(pages)
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

        await self.record_mutation_event(
            "supersede_page", old_id, payload,
        )

        if not self.staged:
            await self._execute(
                self.client.table("pages").update(
                    {
                        "is_superseded": True,
                        "superseded_by": new_id,
                    }
                ).eq("id", old_id)
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
        if self.project_id:
            query = query.eq("project_id", self.project_id)
        if workspace:
            query = query.eq("workspace", workspace.value)
        if page_type:
            query = query.eq("page_type", page_type.value)
        if active_only:
            query = query.eq("is_superseded", False)
        if search:
            query = query.or_(
                f"headline.ilike.%{search}%,content.ilike.%{search}%"
            )
        query = self._staged_filter(query)
        query = query.order(
            "is_human_created", desc=True,
        ).order("created_at", desc=True)
        end = offset + limit - 1
        result = await self._execute(query.range(offset, end))
        total = result.count or 0
        pages = [_row_to_page(r) for r in _rows(result)]
        pages = await self._apply_page_events(pages)
        await self.apply_epistemic_overrides(pages)
        if active_only:
            pages = [p for p in pages if p.is_active()]
        return pages, total


    async def resolve_supersession_chain(
        self, page_id: str, max_depth: int = 10,
    ) -> Page | None:
        """Follow superseded_by links from *page_id* to the final active page.

        Returns the end-of-chain (non-superseded) page, or ``None`` if the
        chain is broken (missing page) or exceeds *max_depth*.
        """
        current_id = page_id
        seen: set[str] = set()
        for _ in range(max_depth):
            page = await self.get_page(current_id)
            if page is None:
                return None
            if not page.is_superseded:
                return page if current_id != page_id else None
            if page.superseded_by is None:
                return None
            if page.superseded_by in seen:
                return None
            seen.add(current_id)
            current_id = page.superseded_by
        return None

    async def resolve_supersession_chains(
        self, page_ids: Sequence[str], max_depth: int = 10,
    ) -> dict[str, Page]:
        """Bulk-resolve supersession chains for multiple page IDs.

        Returns ``{original_id: active_replacement}`` for each input ID whose
        chain reaches an active page. IDs that are already active, have broken
        chains, or exceed *max_depth* are omitted.
        """
        pages = await self.get_pages_by_ids(list(page_ids))
        pending: dict[str, str] = {}
        result: dict[str, Page] = {}
        origin: dict[str, str] = {}

        for pid in page_ids:
            page = pages.get(pid)
            if not page or not page.is_superseded or not page.superseded_by:
                continue
            pending[pid] = page.superseded_by
            origin[pid] = pid

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

    # --- Links ---

    async def save_link(self, link: PageLink) -> None:
        log.debug(
            "save_link: %s -> %s, type=%s",
            link.from_page_id[:8], link.to_page_id[:8], link.link_type.value,
        )
        await self._execute(
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
                    "created_at": link.created_at.isoformat(),
                    "run_id": self.run_id,
                    "staged": self.staged,
                }
            )
        )

    async def get_link(self, link_id: str) -> PageLink | None:
        query = self.client.table("page_links").select("*").eq("id", link_id)
        query = self._staged_filter(query)
        rows = _rows(await self._execute(query))
        if not rows:
            return None
        links = await self._apply_link_events([_row_to_link(rows[0])])
        return links[0] if links else None

    async def get_links_to(self, page_id: str) -> list[PageLink]:
        query = self.client.table("page_links").select("*").eq("to_page_id", page_id)
        query = self._staged_filter(query)
        rows = _rows(await self._execute(query))
        return await self._apply_link_events([_row_to_link(r) for r in rows])

    async def get_links_from(self, page_id: str) -> list[PageLink]:
        query = self.client.table("page_links").select("*").eq("from_page_id", page_id)
        query = self._staged_filter(query)
        rows = _rows(await self._execute(query))
        return await self._apply_link_events([_row_to_link(r) for r in rows])

    async def get_links_from_many(
        self, page_ids: Sequence[str],
    ) -> dict[str, list[PageLink]]:
        """Bulk-fetch outgoing links for many pages. Returns {page_id: [links]}."""
        result: dict[str, list[PageLink]] = {pid: [] for pid in page_ids}
        if not page_ids:
            return result
        id_list = list(dict.fromkeys(page_ids))
        batch_size = 200
        all_links: list[PageLink] = []
        for start in range(0, len(id_list), batch_size):
            batch = id_list[start:start + batch_size]
            query = (
                self.client.table("page_links")
                .select("*")
                .in_("from_page_id", batch)
            )
            query = self._staged_filter(query)
            rows = _rows(await self._execute(query))
            all_links.extend(_row_to_link(r) for r in rows)
        applied = await self._apply_link_events(all_links)
        for link in applied:
            result.setdefault(link.from_page_id, []).append(link)
        return result

    async def get_latest_summary_for_question(self, question_id: str) -> "Page | None":
        """Return the most recent active SUMMARY page linked to a question."""
        links = await self.get_links_to(question_id)
        summary_links = [l for l in links if l.link_type == LinkType.SUMMARIZES]
        if not summary_links:
            return None
        pages = await self.get_pages_by_ids(
            [l.from_page_id for l in summary_links]
        )
        candidates = [
            pages[l.from_page_id]
            for l in summary_links
            if l.from_page_id in pages
            and pages[l.from_page_id].is_active()
            and pages[l.from_page_id].page_type == PageType.SUMMARY
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda p: p.created_at)

    async def get_considerations_for_question(
        self,
        question_id: str,
    ) -> list[tuple[Page, PageLink]]:
        """Return (claim_page, link) pairs for all considerations on a question."""
        links = await self.get_links_to(question_id)
        consideration_links = [
            l for l in links if l.link_type == LinkType.CONSIDERATION
        ]
        if not consideration_links:
            return []
        pages = await self.get_pages_by_ids(
            [l.from_page_id for l in consideration_links]
        )
        return [
            (pages[l.from_page_id], l)
            for l in consideration_links
            if l.from_page_id in pages and pages[l.from_page_id].is_active()
        ]

    async def get_parent_question(self, question_id: str) -> Page | None:
        """Return the parent question, or None if this is a root question."""
        links = await self.get_links_to(question_id)
        for link in links:
            if link.link_type == LinkType.CHILD_QUESTION:
                page = await self.get_page(link.from_page_id)
                if page and page.is_active():
                    return page
        return None

    async def get_child_questions(self, parent_id: str) -> list[Page]:
        """Return sub-questions of a question."""
        links = await self.get_links_from(parent_id)
        child_links = [l for l in links if l.link_type == LinkType.CHILD_QUESTION]
        if not child_links:
            return []
        pages = await self.get_pages_by_ids(
            [l.to_page_id for l in child_links]
        )
        return [
            pages[l.to_page_id]
            for l in child_links
            if l.to_page_id in pages and pages[l.to_page_id].is_active()
        ]

    async def get_child_questions_with_links(
        self, parent_id: str,
    ) -> list[tuple[Page, PageLink]]:
        """Return (child_page, link) pairs for sub-questions of a question."""
        links = await self.get_links_from(parent_id)
        child_links = [l for l in links if l.link_type == LinkType.CHILD_QUESTION]
        if not child_links:
            return []
        pages = await self.get_pages_by_ids(
            [l.to_page_id for l in child_links]
        )
        return [
            (pages[l.to_page_id], l)
            for l in child_links
            if l.to_page_id in pages and pages[l.to_page_id].is_active()
        ]

    async def get_judgements_for_question(self, question_id: str) -> list[Page]:
        links = await self.get_links_to(question_id)
        judgement_links = [l for l in links if l.link_type == LinkType.RELATED]
        if not judgement_links:
            return []
        pages = await self.get_pages_by_ids(
            [l.from_page_id for l in judgement_links]
        )
        return [
            pages[l.from_page_id]
            for l in judgement_links
            if l.from_page_id in pages
            and pages[l.from_page_id].is_active()
            and pages[l.from_page_id].page_type == PageType.JUDGEMENT
        ]

    async def get_judgements_for_questions(
        self, question_ids: Sequence[str],
    ) -> dict[str, list[Page]]:
        """Bulk-fetch active judgements for many questions. Returns {question_id: [judgements]}.

        Issues two batched queries (links + pages) regardless of input size.
        """
        result: dict[str, list[Page]] = {qid: [] for qid in question_ids}
        if not question_ids:
            return result
        id_list = list(dict.fromkeys(question_ids))
        batch_size = 200
        all_links: list[PageLink] = []
        for start in range(0, len(id_list), batch_size):
            batch = id_list[start:start + batch_size]
            query = (
                self.client.table("page_links")
                .select("*")
                .in_("to_page_id", batch)
                .eq("link_type", LinkType.RELATED.value)
            )
            query = self._staged_filter(query)
            rows = _rows(await self._execute(query))
            all_links.extend(_row_to_link(r) for r in rows)
        applied = await self._apply_link_events(all_links)
        from_ids = list({l.from_page_id for l in applied})
        pages = await self.get_pages_by_ids(from_ids)
        for link in applied:
            page = pages.get(link.from_page_id)
            if (
                page is not None
                and page.is_active()
                and page.page_type == PageType.JUDGEMENT
            ):
                result.setdefault(link.to_page_id, []).append(page)
        return result

    async def get_dependents(
        self, page_id: str,
    ) -> list[tuple[Page, PageLink]]:
        """Return (dependent_page, link) for all pages that depend on this one."""
        links = await self.get_links_to(page_id)
        dep_links = [l for l in links if l.link_type == LinkType.DEPENDS_ON]
        if not dep_links:
            return []
        pages = await self.get_pages_by_ids(
            [l.from_page_id for l in dep_links]
        )
        return [
            (pages[l.from_page_id], l)
            for l in dep_links
            if l.from_page_id in pages and pages[l.from_page_id].is_active()
        ]

    async def get_dependencies(
        self, page_id: str,
    ) -> list[tuple[Page, PageLink]]:
        """Return (dependency_page, link) for all pages this one depends on."""
        links = await self.get_links_from(page_id)
        dep_links = [l for l in links if l.link_type == LinkType.DEPENDS_ON]
        if not dep_links:
            return []
        pages = await self.get_pages_by_ids(
            [l.to_page_id for l in dep_links]
        )
        return [
            (pages[l.to_page_id], l)
            for l in dep_links
            if l.to_page_id in pages
        ]

    async def get_stale_dependencies(self) -> list[tuple[PageLink, int | None]]:
        """Return DEPENDS_ON links where the dependency has been superseded.

        Returns (link, change_magnitude) pairs. change_magnitude comes from
        the supersession mutation event if available, otherwise None.
        """
        query = (
            self.client.table("page_links")
            .select("*")
            .eq("link_type", "depends_on")
        )
        query = self._staged_filter(query)
        rows = _rows(await self._execute(query))
        links = await self._apply_link_events([_row_to_link(r) for r in rows])

        stale: list[tuple[PageLink, int | None]] = []
        for link in links:
            dep_page = await self.get_page(link.to_page_id)
            if dep_page and dep_page.is_superseded:
                magnitude = await self._get_supersession_magnitude(dep_page.id)
                stale.append((link, magnitude))
        return stale

    async def get_dependency_counts(self) -> dict[str, int]:
        """Return a map from page_id to how many pages depend on it."""
        query = (
            self.client.table("page_links")
            .select("to_page_id")
            .eq("link_type", LinkType.DEPENDS_ON.value)
        )
        query = self._staged_filter(query)
        rows = _rows(await self._execute(query))
        counts: dict[str, int] = {}
        for row in rows:
            pid = row["to_page_id"]
            counts[pid] = counts.get(pid, 0) + 1
        return counts

    async def _get_supersession_magnitude(self, page_id: str) -> int | None:
        """Look up the change_magnitude from the supersession mutation event."""
        query = (
            self.client.table("mutation_events")
            .select("payload")
            .eq("target_id", page_id)
            .eq("event_type", "supersede_page")
            .order("created_at", desc=True)
            .limit(1)
        )
        rows = _rows(await self._execute(query))
        if rows:
            payload = rows[0].get("payload", {})
            return payload.get("change_magnitude")
        return None

    # --- Calls ---

    async def create_call(
        self,
        call_type: CallType,
        scope_page_id: str | None = None,
        parent_call_id: str | None = None,
        budget_allocated: int | None = None,
        workspace: Workspace = Workspace.RESEARCH,
        context_page_ids: list | None = None,
        call_id: str | None = None,
        sequence_id: str | None = None,
        sequence_position: int | None = None,
    ) -> Call:
        log.debug(
            "create_call: type=%s, scope=%s, parent=%s, budget=%s",
            call_type.value,
            scope_page_id[:8] if scope_page_id else None,
            parent_call_id[:8] if parent_call_id else None,
            budget_allocated,
        )
        call = Call(
            call_type=call_type,
            workspace=workspace,
            scope_page_id=scope_page_id,
            parent_call_id=parent_call_id,
            budget_allocated=budget_allocated,
            status=CallStatus.PENDING,
            context_page_ids=context_page_ids or [],
            sequence_id=sequence_id,
            sequence_position=sequence_position,
        )
        if call_id is not None:
            call.id = call_id
        await self.save_call(call)
        return call

    async def save_call(self, call: Call) -> None:
        if not call.project_id:
            call.project_id = self.project_id
        await self._execute(
            self.client.table("calls").upsert(
                {
                    "id": call.id,
                    "call_type": call.call_type.value,
                    "workspace": call.workspace.value,
                    "project_id": call.project_id,
                    "status": call.status.value,
                    "parent_call_id": call.parent_call_id,
                    "scope_page_id": call.scope_page_id,
                    "budget_allocated": call.budget_allocated,
                    "budget_used": call.budget_used,
                    "context_page_ids": call.context_page_ids,
                    "result_summary": call.result_summary,
                    "review_json": call.review_json,
                    "call_params": call.call_params,
                    "created_at": call.created_at.isoformat(),
                    "completed_at": (
                        call.completed_at.isoformat() if call.completed_at else None
                    ),
                    "run_id": self.run_id,
                    "sequence_id": call.sequence_id,
                    "sequence_position": call.sequence_position,
                    "cost_usd": call.cost_usd,
                }
            )
        )

    async def get_call(self, call_id: str) -> Call | None:
        rows = _rows(
            await self._execute(
                self.client.table("calls").select("*").eq("id", call_id)
            )
        )
        return _row_to_call(rows[0]) if rows else None

    async def update_call_status(
        self,
        call_id: str,
        status: CallStatus,
        result_summary: str = "",
        call_params: dict | None = None,
    ) -> None:
        completed_at = (
            datetime.now(timezone.utc).isoformat()
            if status == CallStatus.COMPLETE
            else None
        )
        payload: dict = {
            "status": status.value,
            "result_summary": result_summary,
            "completed_at": completed_at,
        }
        if call_params is not None:
            payload["call_params"] = call_params
        await self._execute(
            self.client.table("calls").update(
                payload
            ).eq("id", call_id)
        )

    async def increment_call_budget_used(
        self,
        call_id: str,
        amount: int = 1,
    ) -> None:
        await self._execute(
            self.client.rpc(
                "increment_call_budget_used",
                {"call_id": call_id, "amount": amount},
            )
        )

    # --- Per-run budget ---

    async def init_budget(self, total: int) -> None:
        await self._execute(
            self.client.table("budget").upsert(
                {
                    "run_id": self.run_id,
                    "total": total,
                    "used": 0,
                }
            )
        )

    async def get_budget(self) -> tuple[int, int]:
        """Returns (total, used)."""
        rows = _rows(
            await self._execute(
                self.client.table("budget")
                .select("total, used")
                .eq("run_id", self.run_id)
            )
        )
        if rows:
            return rows[0]["total"], rows[0]["used"]
        return 0, 0

    async def consume_budget(self, amount: int = 1) -> bool:
        """Deduct from global budget. Returns False if insufficient budget."""
        result = await self._execute(
            self.client.rpc(
                "consume_budget",
                {"rid": self.run_id, "amount": amount},
            )
        )
        ok = cast(bool, result.data)
        log.debug("consume_budget: amount=%d, success=%s", amount, ok)
        return ok

    async def add_budget(self, amount: int) -> None:
        """Add more calls to the existing budget (for continue runs)."""
        await self._execute(
            self.client.rpc(
                "add_budget",
                {"rid": self.run_id, "amount": amount},
            )
        )

    async def budget_remaining(self) -> int:
        total, used = await self.get_budget()
        return max(0, total - used)

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
        query = self._staged_filter(query)
        rows = _rows(await self._execute(query))
        return await self._apply_link_events([_row_to_link(r) for r in rows])

    async def get_all_links(
        self, page_ids: set[str] | None = None,
    ) -> list[PageLink]:
        """Bulk-fetch links, scoped to a set of page IDs if provided.

        When *page_ids* is given, only links where at least one endpoint is in
        the set are returned. This avoids fetching every link in the DB when the
        caller already knows which pages matter.
        """
        if page_ids is not None:
            return await self._get_links_for_pages(page_ids)
        query = self.client.table("page_links").select("*")
        query = self._staged_filter(query)
        if self.project_id:
            page_ids_query = self._staged_filter(
                self.client.table("pages")
                .select("id")
                .eq("project_id", self.project_id)
            )
            page_ids_rows = _rows(await self._execute(page_ids_query.limit(50000)))
            proj_page_ids = {r["id"] for r in page_ids_rows}
            rows = _rows(await self._execute(query.limit(50000)))
            links = [
                _row_to_link(r) for r in rows
                if r["from_page_id"] in proj_page_ids or r["to_page_id"] in proj_page_ids
            ]
        else:
            rows = _rows(await self._execute(query.limit(50000)))
            links = [_row_to_link(r) for r in rows]
        return await self._apply_link_events(links)

    async def _get_links_for_pages(
        self, page_ids: set[str],
    ) -> list[PageLink]:
        """Fetch links where at least one endpoint is in *page_ids*.

        Batches into chunks to stay within URL-length limits.
        """
        all_links: dict[str, PageLink] = {}
        id_list = list(page_ids)
        batch_size = 200
        for start in range(0, len(id_list), batch_size):
            batch = id_list[start:start + batch_size]
            for col in ('from_page_id', 'to_page_id'):
                query = self.client.table("page_links").select("*").in_(col, batch)
                query = self._staged_filter(query)
                rows = _rows(await self._execute(query.limit(50000)))
                for r in rows:
                    link = _row_to_link(r)
                    all_links[link.id] = link
        return await self._apply_link_events(list(all_links.values()))

    async def delete_link(self, link_id: str) -> None:
        """Delete a page link by ID."""
        rows = _rows(await self._execute(
            self._staged_filter(
                self.client.table("page_links").select("*").eq("id", link_id)
            )
        ))
        link_snapshot = rows[0] if rows else {}
        await self.record_mutation_event("delete_link", link_id, link_snapshot)
        if not self.staged:
            await self._execute(
                self.client.table("page_links").delete().eq("id", link_id)
            )

    async def update_link_role(self, link_id: str, role: LinkRole) -> None:
        """Update a link's role."""
        link = await self.get_link(link_id)
        old_role = link.role.value if link else None
        await self.record_mutation_event(
            "change_link_role", link_id,
            {"new_role": role.value, "old_role": old_role},
        )
        if not self.staged:
            await self._execute(
                self.client.table("page_links").update(
                    {"role": role.value}
                ).eq("id", link_id)
            )

    async def get_last_find_considerations_info(
        self,
        question_id: str,
    ) -> tuple[str, int | None] | None:
        """Return (completed_at_iso, remaining_fruit) for the most recent
        find_considerations call on this question, or None if never run."""
        rows = _rows(
            await self._execute(
                self.client.table("calls")
                .select("completed_at, review_json")
                .eq("call_type", CallType.FIND_CONSIDERATIONS.value)
                .eq("scope_page_id", question_id)
                .eq("status", "complete")
                .order("completed_at", desc=True)
                .limit(1)
            )
        )
        if not rows or not rows[0]["completed_at"]:
            return None
        row = rows[0]
        review = row["review_json"] or {}
        fruit = review.get("remaining_fruit") if isinstance(review, dict) else None
        return row["completed_at"], fruit

    async def get_call_counts_by_type(
        self,
        question_id: str,
    ) -> dict[str, int]:
        """Count completed calls by call_type for a question."""
        rows = _rows(
            await self._execute(
                self.client.table("calls")
                .select("call_type")
                .eq("scope_page_id", question_id)
                .eq("status", "complete")
            )
        )
        counts: dict[str, int] = {}
        for row in rows:
            ct = row["call_type"]
            counts[ct] = counts.get(ct, 0) + 1
        return counts

    async def get_latest_scout_fruit(
        self,
        question_id: str,
    ) -> dict[str, int | None]:
        """Return {call_type: remaining_fruit} for the most recent completed
        scout call of each type on this question."""
        rows = _rows(
            await self._execute(
                self.client.table("calls")
                .select("call_type, completed_at, review_json")
                .eq("scope_page_id", question_id)
                .eq("status", "complete")
                .like("call_type", "scout_%")
                .order("completed_at", desc=True)
            )
        )
        result: dict[str, int | None] = {}
        for row in rows:
            ct = row["call_type"]
            if ct in result:
                continue
            review = row.get("review_json") or {}
            fruit = review.get("remaining_fruit") if isinstance(review, dict) else None
            result[ct] = fruit
        return result

    async def get_ingest_history(self) -> dict[str, list[str]]:
        """Return {source_id: [question_id, ...]} based on considerations
        created by ingest calls."""
        params: dict[str, Any] = {}
        if self.project_id:
            params["pid"] = self.project_id
        rows = _rows(await self._execute(self.client.rpc("get_ingest_history", params)))
        out: dict[str, list[str]] = {}
        for row in rows:
            out.setdefault(row["source_id"], []).append(row["question_id"])
        return out

    # --- Traces ---

    async def save_call_trace(self, call_id: str, events: list[dict]) -> None:
        """Append trace events to the call's trace_json column."""
        await self._execute(
            self.client.rpc(
                "append_call_trace",
                {"cid": call_id, "new_events": events},
            )
        )

    async def get_call_trace(self, call_id: str) -> list[dict]:
        """Fetch trace events for a call."""
        rows = _rows(
            await self._execute(
                self.client.table("calls")
                .select("trace_json")
                .eq("id", call_id)
            )
        )
        if rows and rows[0].get("trace_json"):
            return rows[0]["trace_json"]
        return []

    async def get_child_calls(self, parent_call_id: str) -> list[Call]:
        """Fetch direct child calls ordered by created_at."""
        rows = _rows(
            await self._execute(
                self.client.table("calls")
                .select("*")
                .eq("parent_call_id", parent_call_id)
                .order("created_at")
            )
        )
        return [_row_to_call(r) for r in rows]

    async def create_call_sequence(
        self,
        parent_call_id: str | None,
        scope_question_id: str | None,
        position_in_batch: int = 0,
    ) -> CallSequence:
        seq = CallSequence(
            parent_call_id=parent_call_id,
            run_id=self.run_id,
            scope_question_id=scope_question_id,
            position_in_batch=position_in_batch,
        )
        await self._execute(
            self.client.table("call_sequences").insert(
                {
                    "id": seq.id,
                    "parent_call_id": seq.parent_call_id,
                    "run_id": seq.run_id,
                    "scope_question_id": seq.scope_question_id,
                    "position_in_batch": seq.position_in_batch,
                    "created_at": seq.created_at.isoformat(),
                }
            )
        )
        return seq

    async def get_sequences_for_call(
        self, parent_call_id: str,
    ) -> Sequence[CallSequence]:
        """Fetch sequences for a parent call, ordered by position_in_batch."""
        rows = _rows(
            await self._execute(
                self.client.table("call_sequences")
                .select("*")
                .eq("parent_call_id", parent_call_id)
                .order("position_in_batch")
            )
        )
        return [_row_to_call_sequence(r) for r in rows]

    async def get_calls_for_sequence(
        self, sequence_id: str,
    ) -> Sequence[Call]:
        """Fetch calls in a sequence, ordered by sequence_position."""
        rows = _rows(
            await self._execute(
                self.client.table("calls")
                .select("*")
                .eq("sequence_id", sequence_id)
                .order("sequence_position")
            )
        )
        return [_row_to_call(r) for r in rows]

    async def get_root_calls_for_question(self, question_id: str) -> list[Call]:
        """Find top-level calls for a question (prioritization calls with no
        parent, or whose parent targets a different question)."""
        rows = _rows(
            await self._execute(
                self.client.table("calls")
                .select("*")
                .eq("scope_page_id", question_id)
                .is_("parent_call_id", "null")
                .order("created_at")
            )
        )
        result = [_row_to_call(r) for r in rows]
        if result:
            return result
        # Fallback: return all calls scoped to this question
        rows = _rows(
            await self._execute(
                self.client.table("calls")
                .select("*")
                .eq("scope_page_id", question_id)
                .order("created_at")
            )
        )
        return [_row_to_call(r) for r in rows]

    async def save_page_rating(
        self,
        page_id: str,
        call_id: str,
        score: int,
        note: str = "",
    ) -> None:
        await self._execute(
            self.client.table("page_ratings").insert(
                {
                    "id": str(uuid.uuid4()),
                    "page_id": page_id,
                    "call_id": call_id,
                    "score": score,
                    "note": note,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "run_id": self.run_id,
                }
            )
        )

    async def save_page_flag(
        self,
        flag_type: str,
        call_id: str | None = None,
        note: str = "",
        page_id: str | None = None,
        page_id_a: str | None = None,
        page_id_b: str | None = None,
    ) -> None:
        await self._execute(
            self.client.table("page_flags").insert(
                {
                    "id": str(uuid.uuid4()),
                    "flag_type": flag_type,
                    "call_id": call_id,
                    "page_id": page_id,
                    "page_id_a": page_id_a,
                    "page_id_b": page_id_b,
                    "note": note,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "run_id": self.run_id,
                }
            )
        )

    async def save_epistemic_score(
        self,
        page_id: str,
        call_id: str,
        credence: int,
        robustness: int,
        reasoning: str = "",
        source_page_id: str | None = None,
    ) -> None:
        row: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "page_id": page_id,
            "call_id": call_id,
            "credence": credence,
            "robustness": robustness,
            "reasoning": reasoning,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
        }
        if source_page_id is not None:
            row["source_page_id"] = source_page_id
        await self._execute(
            self.client.table("epistemic_scores").insert(row)
        )

    async def apply_epistemic_overrides(self, pages: Sequence[Page]) -> None:
        """Override credence/robustness on pages with latest epistemic_scores."""
        if not pages:
            return
        page_ids = [p.id for p in pages]
        batch_size = 200
        rows: list[dict[str, Any]] = []
        for i in range(0, len(page_ids), batch_size):
            batch = page_ids[i : i + batch_size]
            rows.extend(
                _rows(
                    await self._execute(
                        self.client.table("epistemic_scores")
                        .select("page_id,credence,robustness,created_at")
                        .in_("page_id", batch)
                        .eq("run_id", self.run_id)
                        .order("created_at", desc=True)
                    )
                )
            )
        seen: set[str] = set()
        overrides: dict[str, tuple[int, int]] = {}
        for row in rows:
            pid = row["page_id"]
            if pid not in seen:
                seen.add(pid)
                overrides[pid] = (row["credence"], row["robustness"])
        for page in pages:
            if page.id in overrides:
                page.credence, page.robustness = overrides[page.id]

    async def get_epistemic_score_source(
        self,
        page_id: str,
    ) -> tuple[dict[str, Any] | None, Page | None]:
        """Return the latest epistemic score entry and its source judgement (if any).

        Returns (score_row, judgement_page) where either or both may be None.
        """
        rows = _rows(
            await self._execute(
                self.client.table("epistemic_scores")
                .select("*")
                .eq("page_id", page_id)
                .eq("run_id", self.run_id)
                .order("created_at", desc=True)
                .limit(1)
            )
        )
        if not rows:
            return None, None
        score_row = rows[0]
        call_id = score_row["call_id"]
        judgement_rows = _rows(
            await self._execute(
                self.client.table("pages")
                .select("*")
                .eq("provenance_call_id", call_id)
                .eq("page_type", PageType.JUDGEMENT.value)
                .eq("is_superseded", False)
                .order("created_at", desc=True)
                .limit(1)
            )
        )
        judgement = _row_to_page(judgement_rows[0]) if judgement_rows else None
        return score_row, judgement

    async def get_latest_judgement_for_call(
        self,
        call_id: str,
    ) -> str | None:
        """Return the page ID of the most recent judgement created by a call."""
        rows = _rows(
            await self._execute(
                self.client.table("pages")
                .select("id")
                .eq("provenance_call_id", call_id)
                .eq("page_type", PageType.JUDGEMENT.value)
                .order("created_at", desc=True)
                .limit(1)
            )
        )
        return rows[0]["id"] if rows else None

    async def get_root_questions(
        self,
        workspace: Workspace = Workspace.RESEARCH,
    ) -> list[Page]:
        """Return questions that have no parent (top-level questions)."""
        params: dict[str, Any] = {"ws": workspace.value}
        if self.project_id:
            params["pid"] = self.project_id
        if self.staged:
            params["p_staged_run_id"] = self.run_id
        rows = _rows(
            await self._execute(self.client.rpc("get_root_questions", params))
        )
        pages = [_row_to_page(r) for r in rows]
        await self.apply_epistemic_overrides(pages)
        return pages

    async def get_human_questions(
        self,
        workspace: Workspace = Workspace.RESEARCH,
    ) -> list[Page]:
        """Return all active, human-authored questions in *workspace*.

        Uses the generated `is_human_created` column on `pages`. Unlike
        `get_root_questions`, this does not assume the question graph is a
        DAG -- a human-authored question deep inside a cycle is still
        returned.
        """
        query = (
            self.client.table("pages")
            .select("*")
            .eq("page_type", PageType.QUESTION.value)
            .eq("workspace", workspace.value)
            .eq("is_human_created", True)
            .eq("is_superseded", False)
        )
        if self.project_id:
            query = query.eq("project_id", self.project_id)
        query = self._staged_filter(query)
        rows = _rows(await self._execute(query))
        pages = [_row_to_page(r) for r in rows]
        pages = await self._apply_page_events(pages)
        await self.apply_epistemic_overrides(pages)
        return [p for p in pages if p.is_active()]

    async def count_pages_for_question(self, question_id: str) -> dict:
        """Count pages linked to or created in context of a question."""
        cons_result = await self._execute(
            self.client.table("page_links")
            .select("id", count=CountMethod.exact)
            .eq("to_page_id", question_id)
            .eq("link_type", "consideration")
        )
        judgements_result = await self._execute(
            self.client.rpc(
                "count_active_judgements",
                {"qid": question_id},
            )
        )
        return {
            "considerations": cons_result.count or 0,
            "judgements": cast(int, judgements_result.data or 0),
        }

    async def save_llm_exchange(
        self,
        call_id: str,
        phase: str,
        system_prompt: str | None,
        user_message: str | None,
        response_text: str | None,
        tool_calls: list[dict] | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        error: str | None = None,
        duration_ms: int | None = None,
        round_num: int | None = None,
        cache_creation_input_tokens: int | None = None,
        cache_read_input_tokens: int | None = None,
        user_messages: list[dict] | None = None,
    ) -> str:
        exchange_id = str(uuid.uuid4())
        row: dict[str, Any] = {
            "id": exchange_id,
            "call_id": call_id,
            "run_id": self.run_id,
            "phase": phase,
            "round": round_num,
            "system_prompt": system_prompt,
            "user_message": user_message,
            "response_text": response_text,
            "tool_calls": tool_calls or [],
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "error": error,
            "duration_ms": duration_ms,
            "cache_creation_input_tokens": cache_creation_input_tokens,
            "cache_read_input_tokens": cache_read_input_tokens,
        }
        if user_messages is not None:
            row["user_messages"] = user_messages
        await self._execute(self.client.table("call_llm_exchanges").insert(row))
        return exchange_id

    async def get_llm_exchanges(self, call_id: str) -> list[dict[str, Any]]:
        rows = _rows(
            await self._execute(
                self.client.table("call_llm_exchanges")
                .select("id, call_id, phase, round, input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens, duration_ms, error, created_at")
                .eq("call_id", call_id)
                .order("round")
            )
        )
        return rows

    async def get_llm_exchange(self, exchange_id: str) -> dict[str, Any] | None:
        rows = _rows(
            await self._execute(
                self.client.table("call_llm_exchanges")
                .select("*")
                .eq("id", exchange_id)
            )
        )
        return rows[0] if rows else None

    async def get_call_rows_for_run(self, run_id: str) -> list[dict]:
        return _rows(
            await self._execute(
                self.client.table("calls")
                .select("*")
                .eq("run_id", run_id)
                .order("created_at")
            )
        )

    async def get_calls_for_run(self, run_id: str) -> list[Call]:
        rows = await self.get_call_rows_for_run(run_id)
        return [_row_to_call(r) for r in rows]

    async def get_run_question_id(self, run_id: str) -> str | None:
        rows = _rows(
            await self._execute(
                self.client.table("calls")
                .select("scope_page_id")
                .eq("run_id", run_id)
                .is_("parent_call_id", "null")
                .order("created_at")
                .limit(1)
            )
        )
        return rows[0]["scope_page_id"] if rows else None

    async def get_run_for_page(self, page_id: str) -> dict[str, Any] | None:
        """Return the run that created a page.

        Looks up via provenance_call_id first. Falls back to finding a
        root call scoped to the page (for root questions that weren't
        created by a call).
        """
        page = await self.get_page(page_id)
        if not page:
            return None
        if page.provenance_call_id:
            rows = _rows(
                await self._execute(
                    self.client.table("calls")
                    .select("run_id, created_at")
                    .eq("id", page.provenance_call_id)
                    .limit(1)
                )
            )
            if rows and rows[0].get("run_id"):
                return {
                    "run_id": rows[0]["run_id"],
                    "created_at": rows[0]["created_at"],
                    "provenance_call_id": page.provenance_call_id,
                }
        rows = _rows(
            await self._execute(
                self.client.table("calls")
                .select("run_id, created_at")
                .eq("scope_page_id", page_id)
                .is_("parent_call_id", "null")
                .order("created_at", desc=True)
                .limit(1)
            )
        )
        if rows and rows[0].get("run_id"):
            return {"run_id": rows[0]["run_id"], "created_at": rows[0]["created_at"]}
        return None

    async def create_run(
        self,
        name: str,
        question_id: str | None,
        config: dict | None = None,
        ab_arm: str | None = None,
    ) -> None:
        """Insert a row in the runs table for this DB's run_id."""
        await self._execute(
            self.client.table("runs").insert(
                {
                    "id": self.run_id,
                    "name": name,
                    "project_id": self.project_id,
                    "question_id": question_id,
                    "config": config or {},
                    "staged": self.staged,
                    "ab_run_id": self.ab_run_id,
                    "ab_arm": ab_arm,
                }
            )
        )

    async def stage_run(self, run_id: str) -> None:
        """Retroactively stage a completed non-staged run.

        Flips the staged flag on the run's rows, then reverts direct
        mutations (supersessions, link deletions, role changes) so baseline
        readers see the pre-run state. The mutation events remain, so a
        staged reader replaying them will see the same view the run
        originally produced.
        """
        await self._execute(
            self.client.table("runs").update({"staged": True}).eq("id", run_id)
        )
        await self._execute(
            self.client.table("pages").update({"staged": True}).eq("run_id", run_id)
        )
        await self._execute(
            self.client.table("page_links")
            .update({"staged": True})
            .eq("run_id", run_id)
        )

        events = _rows(
            await self._execute(
                self.client.table("mutation_events")
                .select("event_type, target_id, payload")
                .eq("run_id", run_id)
                .order("created_at")
            )
        )
        for ev in events:
            et = ev["event_type"]
            tid = ev["target_id"]
            payload = ev.get("payload") or {}

            if et == "supersede_page":
                await self._execute(
                    self.client.table("pages")
                    .update({"is_superseded": False, "superseded_by": None})
                    .eq("id", tid)
                )

            elif et == "delete_link":
                if not payload or "from_page_id" not in payload:
                    log.warning(
                        "Cannot restore deleted link %s: no snapshot in event payload",
                        tid,
                    )
                    continue
                was_own_link = payload.get("run_id") == run_id
                restore_row = {
                    "id": tid,
                    "from_page_id": payload["from_page_id"],
                    "to_page_id": payload["to_page_id"],
                    "link_type": payload["link_type"],
                    "direction": payload.get("direction"),
                    "strength": payload.get("strength", 2.5),
                    "reasoning": payload.get("reasoning", ""),
                    "role": payload.get("role", "direct"),
                    "created_at": payload.get("created_at"),
                    "run_id": payload.get("run_id", run_id),
                    "staged": was_own_link,
                }
                await self._execute(
                    self.client.table("page_links").upsert(restore_row)
                )

            elif et == "change_link_role":
                old_role = payload.get("old_role")
                if not old_role:
                    log.warning(
                        "Cannot revert role change for link %s: no old_role in event payload",
                        tid,
                    )
                    continue
                link_rows = _rows(await self._execute(
                    self.client.table("page_links")
                    .select("run_id")
                    .eq("id", tid)
                ))
                if link_rows and link_rows[0].get("run_id") == run_id:
                    continue
                await self._execute(
                    self.client.table("page_links")
                    .update({"role": old_role})
                    .eq("id", tid)
                )

    async def commit_staged_run(self, run_id: str) -> None:
        """Commit a staged run, making its effects visible to all readers.

        Flips the staged flag to false on the run's rows, then applies
        mutation events (supersessions, link deletions, role changes) that
        were recorded but never written directly to the database.
        """
        run_rows = _rows(
            await self._execute(
                self.client.table("runs").select("id, staged").eq("id", run_id)
            )
        )
        if not run_rows:
            raise ValueError(f"Run {run_id} not found")
        if not run_rows[0].get("staged"):
            raise ValueError(f"Run {run_id} is not staged")

        await self._execute(
            self.client.table("runs").update({"staged": False}).eq("id", run_id)
        )
        await self._execute(
            self.client.table("pages")
            .update({"staged": False})
            .eq("run_id", run_id)
        )
        await self._execute(
            self.client.table("page_links")
            .update({"staged": False})
            .eq("run_id", run_id)
        )

        events = _rows(
            await self._execute(
                self.client.table("mutation_events")
                .select("event_type, target_id, payload")
                .eq("run_id", run_id)
                .order("created_at")
            )
        )
        for ev in events:
            et = ev["event_type"]
            tid = ev["target_id"]
            payload = ev.get("payload") or {}

            if et == "supersede_page":
                await self._execute(
                    self.client.table("pages")
                    .update(
                        {
                            "is_superseded": True,
                            "superseded_by": payload["new_page_id"],
                        }
                    )
                    .eq("id", tid)
                )

            elif et == "delete_link":
                await self._execute(
                    self.client.table("page_links").delete().eq("id", tid)
                )

            elif et == "change_link_role":
                new_role = payload.get("new_role")
                if not new_role:
                    log.warning(
                        "Cannot apply role change for link %s: "
                        "no new_role in event payload",
                        tid,
                    )
                    continue
                await self._execute(
                    self.client.table("page_links")
                    .update({"role": new_role})
                    .eq("id", tid)
                )

    async def create_ab_run(
        self,
        ab_run_id: str,
        name: str,
        question_id: str | None,
    ) -> None:
        """Insert a row in the ab_runs table."""
        await self._execute(
            self.client.table("ab_runs").insert(
                {
                    "id": ab_run_id,
                    "name": name,
                    "project_id": self.project_id,
                    "question_id": question_id,
                }
            )
        )

    async def list_runs_for_project(self, project_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """Return recent runs for a project, newest first.

        Queries the runs table. Groups AB runs into a single entry with
        both arm run_ids. Falls back to the calls table for legacy runs
        that predate the runs table.
        """
        run_rows = _rows(
            await self._execute(
                self.client.table("runs")
                .select("id, name, question_id, config, ab_run_id, ab_arm, created_at, staged")
                .eq("project_id", project_id)
                .order("created_at", desc=True)
                .limit(limit * 2)
            )
        )
        ab_groups: dict[str, dict[str, Any]] = {}
        results: list[dict[str, Any]] = []
        seen_run_ids: set[str] = set()
        for row in run_rows:
            ab_id = row.get("ab_run_id")
            if ab_id:
                if ab_id not in ab_groups:
                    ab_groups[ab_id] = {
                        "ab_run_id": ab_id,
                        "created_at": row["created_at"],
                        "name": row.get("name", ""),
                        "question_summary": None,
                        "arms": {},
                    }
                arm = row.get("ab_arm", "?")
                ab_groups[ab_id]["arms"][arm] = {
                    "run_id": row["id"],
                    "config": row.get("config", {}),
                }
                seen_run_ids.add(row["id"])
            else:
                question_summary = None
                qid = row.get("question_id")
                if qid:
                    page = await self.get_page(qid)
                    if page:
                        question_summary = page.headline
                results.append({
                    "run_id": row["id"],
                    "created_at": row["created_at"],
                    "name": row.get("name", ""),
                    "config": row.get("config", {}),
                    "question_summary": question_summary,
                    "staged": row.get("staged", False),
                })
                seen_run_ids.add(row["id"])
        for ab_group in ab_groups.values():
            qid = None
            for arm_info in ab_group["arms"].values():
                rid = arm_info["run_id"]
                q = await self.get_run_question_id(rid)
                if q:
                    qid = q
                    break
            if qid:
                page = await self.get_page(qid)
                if page:
                    ab_group["question_summary"] = page.headline
            results.append(ab_group)
        # Fallback: include legacy runs from calls table that don't have a runs row
        legacy_rows = _rows(
            await self._execute(
                self.client.table("calls")
                .select("run_id, created_at, scope_page_id")
                .eq("project_id", project_id)
                .is_("parent_call_id", "null")
                .order("created_at", desc=True)
            )
        )
        seen_legacy: set[str] = set()
        for row in legacy_rows:
            rid = row.get("run_id")
            if not rid or rid in seen_run_ids or rid in seen_legacy:
                continue
            seen_legacy.add(rid)
            question_summary = None
            scope_id = row.get("scope_page_id")
            if scope_id:
                page = await self.get_page(scope_id)
                if page:
                    question_summary = page.headline
            results.append({
                "run_id": rid,
                "created_at": row["created_at"],
                "question_summary": question_summary,
            })
        results.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        return results[:limit]

    async def delete_run_data(self, delete_project: bool = False) -> None:
        """Delete all data for this run_id. Used by test teardown."""
        await self._execute(
            self.client.table("mutation_events").delete().eq(
                "run_id", self.run_id
            )
        )
        await self._execute(
            self.client.table("call_llm_exchanges").delete().eq(
                "run_id", self.run_id
            )
        )
        for table in ["page_flags", "page_ratings", "page_links"]:
            await self._execute(
                self.client.table(table).delete().eq("run_id", self.run_id)
            )
        # Null out sequence_id FK before deleting sequences and calls
        await self._execute(
            self.client.table("calls").update(
                {"sequence_id": None}
            ).eq("run_id", self.run_id)
        )
        await self._execute(
            self.client.table("call_sequences").delete().eq(
                "run_id", self.run_id
            )
        )
        for table in ["calls", "pages"]:
            await self._execute(
                self.client.table(table).delete().eq("run_id", self.run_id)
            )
        await self._execute(
            self.client.table("budget").delete().eq("run_id", self.run_id)
        )
        await self._execute(
            self.client.table("runs").delete().eq("id", self.run_id)
        )
        if self.ab_run_id:
            # Only delete ab_run if no other runs reference it
            remaining = _rows(
                await self._execute(
                    self.client.table("runs")
                    .select("id")
                    .eq("ab_run_id", self.ab_run_id)
                    .limit(1)
                )
            )
            if not remaining:
                await self._execute(
                    self.client.table("ab_runs").delete().eq(
                        "id", self.ab_run_id
                    )
                )
        if delete_project and self.project_id:
            await self._execute(
                self.client.table("projects").delete().eq(
                    "id", self.project_id
                )
            )
