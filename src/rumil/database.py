"""
Supabase database layer for the research workspace.
"""

import asyncio
import logging
import time
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast

import httpx
from postgrest.exceptions import APIError
from postgrest.types import CountMethod
from supabase.lib.client_options import AsyncClientOptions
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception,
    wait_exponential,
)

# MutationState, _row_to_*, _rows, and the column constants now live in
# rumil.db. They are re-exported here so existing callers that do
# ``from rumil.database import _row_to_page`` (etc.) keep working.
from rumil.db.mutation_log import MutationState as MutationState
from rumil.db.row_helpers import _LINK_COLUMNS as _LINK_COLUMNS
from rumil.db.row_helpers import _SLIM_PAGE_COLUMNS as _SLIM_PAGE_COLUMNS
from rumil.db.row_helpers import _row_to_annotation_event as _row_to_annotation_event
from rumil.db.row_helpers import _row_to_call as _row_to_call
from rumil.db.row_helpers import _row_to_call_sequence as _row_to_call_sequence
from rumil.db.row_helpers import _row_to_link as _row_to_link
from rumil.db.row_helpers import _row_to_page as _row_to_page
from rumil.db.row_helpers import _row_to_suggestion as _row_to_suggestion
from rumil.db.row_helpers import _Rows as _Rows
from rumil.db.row_helpers import _rows as _rows
from rumil.models import (
    AnnotationEvent,
    Call,
    CallSequence,
    CallStatus,
    CallType,
    ChatConversation,
    ChatMessage,
    ChatMessageRole,
    LinkRole,
    LinkType,
    Page,
    PageLink,
    PageType,
    Project,
    ReputationEvent,
    Suggestion,
    SuggestionStatus,
    Workspace,
)
from rumil.settings import get_settings
from rumil.staged_overlay import StagedOverlay
from supabase import AsyncClient, acreate_client

log = logging.getLogger(__name__)


from rumil.db.eval_summary import (
    EvalSummary,
)
from rumil.db.eval_summary import (
    aggregate_eval_rows_by_subject as _aggregate_eval_rows_by_subject,
)

_DB_RETRYABLE_EXCEPTIONS = (
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.ConnectError,
    httpx.ReadError,
    httpx.RemoteProtocolError,
)


def _is_retryable_api_error(exc: BaseException) -> bool:
    # Gateway/upstream failures (e.g. Cloudflare 502, Supabase 503/504) come back
    # as APIError because postgrest can't parse the HTML error page as JSON.
    # Retry these, but not 4xx errors (auth, constraint violations, etc).
    if not isinstance(exc, APIError):
        return False
    code = exc.code
    if code is None:
        return False
    try:
        status = int(code)
    except (TypeError, ValueError):
        return False
    return 500 <= status < 600


def _should_retry_db_exception(exc: BaseException) -> bool:
    return isinstance(exc, _DB_RETRYABLE_EXCEPTIONS) or _is_retryable_api_error(exc)


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
    retry=retry_if_exception(_should_retry_db_exception),
    stop=_stop_after_db_retries,
    wait=wait_exponential(multiplier=0.5, min=0.5, max=60),
    before_sleep=_log_db_retry,
    reraise=True,
)


class DB:
    def __init__(
        self,
        run_id: str,
        client: AsyncClient,
        project_id: str = "",
        staged: bool = False,
        snapshot_ts: datetime | None = None,
    ):
        from rumil.db.annotation_store import AnnotationStore
        from rumil.db.call_store import CallStore
        from rumil.db.chat_store import ChatStore
        from rumil.db.link_store import LinkStore
        from rumil.db.page_store import PageStore
        from rumil.db.project_store import ProjectStore
        from rumil.db.run_store import RunStore

        self.run_id = run_id
        self.client = client
        self.project_id = project_id
        self.staged = staged
        self.snapshot_ts = snapshot_ts
        self._semaphore = asyncio.Semaphore(get_settings().db_max_concurrent_queries)
        self._prod: bool = False
        self._mutation_cache: MutationState | None = None
        self._mutation_cache_ts: float = 0.0
        self.overlay = StagedOverlay(self)
        self.projects = ProjectStore(self)
        self.runs = RunStore(self)
        self.calls = CallStore(self)
        self.annotations = AnnotationStore(self)
        self.chat = ChatStore(self)
        self.pages = PageStore(self)
        self.links = LinkStore(self)

    @classmethod
    async def create(
        cls,
        run_id: str,
        prod: bool = False,
        project_id: str = "",
        client: AsyncClient | None = None,
        staged: bool = False,
        snapshot_ts: datetime | None = None,
    ) -> "DB":
        """Create a DB handle.

        When ``staged=True`` and no explicit ``snapshot_ts`` is supplied, the
        new handle pins itself to the current server time (via ``now()``) so
        baseline rows and mutation events that land *after* this point are
        invisible to this run. This gives each staged run a fixed view of the
        workspace — the "fork-at-snapshot" contract from
        ``marketplace-thread/11-staging-concurrency.md``.
        """
        if client is None:
            url, key = get_settings().get_supabase_credentials(prod)
            client = await acreate_client(url, key, options=AsyncClientOptions(schema="public"))
        db = cls(
            run_id=run_id,
            client=client,
            project_id=project_id,
            staged=staged,
            snapshot_ts=snapshot_ts,
        )
        db._prod = prod
        if staged and snapshot_ts is None:
            db.snapshot_ts = await db._fetch_db_now()
        return db

    async def _fetch_db_now(self) -> datetime:
        """Return the database server's current timestamp.

        Used to pin a staged run's snapshot boundary to a server-side instant,
        avoiding any local-clock drift between worker and DB.
        """
        try:
            result = await self._execute(self.client.rpc("db_now", {}))
            value = result.data
            if isinstance(value, str):
                return datetime.fromisoformat(value)
        except Exception:
            log.warning("db_now() RPC failed, falling back to local clock", exc_info=True)
        return datetime.now(UTC)

    async def fork(self) -> "DB":
        """Create a new DB instance with a fresh Supabase client.

        Shares run_id, project_id, staged flag, and snapshot_ts with the
        parent but gets its own HTTP connection. Use this to scope
        connections to a single call, avoiding HTTP/2 stream exhaustion on
        long-running jobs.
        """
        url, key = get_settings().get_supabase_credentials(self._prod)
        client = await acreate_client(url, key, options=AsyncClientOptions(schema="public"))
        db = DB(
            run_id=self.run_id,
            client=client,
            project_id=self.project_id,
            staged=self.staged,
            snapshot_ts=self.snapshot_ts,
        )
        db._prod = self._prod
        return db

    async def close(self) -> None:
        """Close the underlying HTTP connections."""
        try:
            await self.client.postgrest.aclose()
        except Exception:
            log.debug("Failed to close postgrest client", exc_info=True)

    @_db_retry
    async def _execute(self, query: Any) -> Any:
        """Execute a query builder through the concurrency semaphore."""
        async with self._semaphore:
            return await query.execute()

    def _staged_filter(self, query: Any) -> Any:
        """Apply staged-run visibility filter to a query.

        Staged runs see baseline (staged=false) + their own rows. When a
        ``snapshot_ts`` is pinned, baseline rows must additionally have been
        created at or before that instant — rows committed by other runs
        after the snapshot are invisible. Own-run rows are always visible
        regardless of timestamp. Non-staged runs see only baseline rows.
        """
        if self.staged:
            if self.snapshot_ts is not None:
                ts = self.snapshot_ts.isoformat()
                return query.or_(
                    f"and(staged.eq.false,created_at.lte.{ts}),run_id.eq.{self.run_id}"
                )
            return query.or_(f"staged.eq.false,run_id.eq.{self.run_id}")
        return query.eq("staged", False)

    _MUTATION_CACHE_TTL_S = 5.0

    async def _load_mutation_state(self) -> MutationState:
        """Fetch and cache mutation events visible to this staged run.

        Own-run events are always forwarded onto the view. When a
        ``snapshot_ts`` is pinned, events committed by *other* runs are
        split two ways:

        - Events with ``created_at <= snapshot_ts`` are forwarded normally —
          the staged run saw the baseline mutation happen within its
          snapshot.
        - Events with ``created_at > snapshot_ts`` (other runs' writes after
          the fork) are recorded as *unapply* entries: the base table was
          dual-written and now shows post-snapshot state, but this staged
          run must not observe those mutations. ``_apply_page_events`` /
          ``_apply_link_events`` use the unapply set to roll the base-table
          values back to their pre-mutation values on read.

        Without a snapshot, only own-run events are included (matching
        pre-fork behavior).
        """
        now = time.monotonic()
        if (
            self._mutation_cache is not None
            and now - self._mutation_cache_ts < self._MUTATION_CACHE_TTL_S
        ):
            return self._mutation_cache
        if not self.staged:
            self._mutation_cache = MutationState()
            self._mutation_cache_ts = now
            return self._mutation_cache
        own_rows = _rows(
            await self._execute(
                self.client.table("mutation_events")
                .select("event_type, target_id, payload, created_at, run_id")
                .eq("run_id", self.run_id)
                .order("created_at")
            )
        )
        baseline_rows: list[dict[str, Any]] = []
        post_snapshot_rows: list[dict[str, Any]] = []
        if self.snapshot_ts is not None:
            ts = self.snapshot_ts.isoformat()
            baseline_rows = _rows(
                await self._execute(
                    self.client.table("mutation_events")
                    .select("event_type, target_id, payload, created_at, run_id")
                    .neq("run_id", self.run_id)
                    .lte("created_at", ts)
                    .order("created_at")
                )
            )
            post_snapshot_rows = _rows(
                await self._execute(
                    self.client.table("mutation_events")
                    .select("event_type, target_id, payload, created_at, run_id")
                    .neq("run_id", self.run_id)
                    .gt("created_at", ts)
                    .order("created_at")
                )
            )
        combined = sorted(
            [*baseline_rows, *own_rows],
            key=lambda r: r.get("created_at") or "",
        )
        state = MutationState()
        for row in combined:
            et = row["event_type"]
            tid = row["target_id"]
            payload = row.get("payload") or {}
            if et == "supersede_page":
                state.superseded_pages[tid] = payload.get("new_page_id", "")
            elif et == "delete_link":
                state.deleted_links.add(tid)
            elif et == "change_link_role":
                state.link_role_overrides[tid] = LinkRole(payload["new_role"])
            elif et == "update_page_content":
                state.page_content_overrides[tid] = payload.get("new_content", "")
            elif et == "set_credence":
                state.credence_overrides[tid] = (
                    payload.get("value"),
                    payload.get("reasoning"),
                )
            elif et == "set_robustness":
                state.robustness_overrides[tid] = (
                    payload.get("value"),
                    payload.get("reasoning"),
                )
        # Post-snapshot baseline events: record how to undo them on read.
        # We iterate oldest-first so that the *earliest* post-snapshot
        # mutation wins for unapply_update_content — its ``old_content`` is
        # the pre-snapshot value. Same logic for role overrides.
        post_sorted = sorted(
            post_snapshot_rows,
            key=lambda r: r.get("created_at") or "",
        )
        for row in post_sorted:
            et = row["event_type"]
            tid = row["target_id"]
            payload = row.get("payload") or {}
            if et == "supersede_page":
                # Only mark as "unapply" if this is not already superseded
                # in the pre-snapshot view.
                if tid not in state.superseded_pages:
                    state.unapply_supersessions.add(tid)
            elif et == "update_page_content":
                if (
                    tid not in state.page_content_overrides
                    and tid not in state.unapply_update_content
                ):
                    state.unapply_update_content[tid] = payload.get("old_content", "")
            elif et == "change_link_role":
                if tid not in state.link_role_overrides and tid not in state.unapply_role_overrides:
                    old_role = payload.get("old_role")
                    if old_role:
                        state.unapply_role_overrides[tid] = LinkRole(old_role)
            elif et == "set_credence":
                if tid not in state.credence_overrides and tid not in state.unapply_credence:
                    if "old_value" in payload:
                        state.unapply_credence[tid] = (
                            payload.get("old_value"),
                            payload.get("old_reasoning"),
                        )
            elif et == "set_robustness":
                if tid not in state.robustness_overrides and tid not in state.unapply_robustness:
                    if "old_value" in payload:
                        state.unapply_robustness[tid] = (
                            payload.get("old_value"),
                            payload.get("old_reasoning"),
                        )
            # Note: baseline delete_link events after snapshot physically
            # remove the row from page_links (non-staged deletes DELETE FROM).
            # Restoring them on read would require reinserting from the
            # event payload; see CLAUDE.md on deletion semantics. For now,
            # deletions landing after the snapshot are visible to the
            # staged run — a deviation from pure fork-at-snapshot, flagged
            # as a known gap.
        self._mutation_cache = state
        self._mutation_cache_ts = now
        return state

    def _invalidate_mutation_cache(self) -> None:
        self._mutation_cache = None
        self._mutation_cache_ts = 0.0

    async def _apply_page_events(self, pages: Sequence[Page]) -> list[Page]:
        """Overlay mutation events onto a batch of pages.

        Applies both forward overlays (supersessions/content updates this
        run should see) and unapply overlays (baseline mutations committed
        after this run's snapshot that the base table reflects but this
        run should not).
        """
        state = await self._load_mutation_state()
        has_any = (
            state.superseded_pages
            or state.page_content_overrides
            or state.unapply_supersessions
            or state.unapply_update_content
            or state.credence_overrides
            or state.robustness_overrides
            or state.unapply_credence
            or state.unapply_robustness
        )
        if not has_any:
            return list(pages)
        result: list[Page] = []
        for p in pages:
            updates: dict = {}
            if p.id in state.superseded_pages:
                updates["is_superseded"] = True
                updates["superseded_by"] = state.superseded_pages[p.id]
            elif p.id in state.unapply_supersessions and p.is_superseded:
                updates["is_superseded"] = False
                updates["superseded_by"] = None
            if p.id in state.page_content_overrides:
                updates["content"] = state.page_content_overrides[p.id]
            elif p.id in state.unapply_update_content:
                updates["content"] = state.unapply_update_content[p.id]
            if p.id in state.credence_overrides:
                value, reasoning = state.credence_overrides[p.id]
                updates["credence"] = value
                updates["credence_reasoning"] = reasoning
            elif p.id in state.unapply_credence:
                value, reasoning = state.unapply_credence[p.id]
                updates["credence"] = value
                updates["credence_reasoning"] = reasoning
            if p.id in state.robustness_overrides:
                value, reasoning = state.robustness_overrides[p.id]
                updates["robustness"] = value
                updates["robustness_reasoning"] = reasoning
            elif p.id in state.unapply_robustness:
                value, reasoning = state.unapply_robustness[p.id]
                updates["robustness"] = value
                updates["robustness_reasoning"] = reasoning
            if updates:
                p = p.model_copy(update=updates)
            result.append(p)
        return result

    async def _apply_link_events(self, links: Sequence[PageLink]) -> list[PageLink]:
        """Overlay mutation events onto a batch of links.

        Applies forward overlays (deletes and role changes this run saw)
        and unapply overlays for role changes that landed after this run's
        snapshot.
        """
        state = await self._load_mutation_state()
        has_any = state.deleted_links or state.link_role_overrides or state.unapply_role_overrides
        if not has_any:
            return list(links)
        result: list[PageLink] = []
        for link in links:
            if link.id in state.deleted_links:
                continue
            if link.id in state.link_role_overrides:
                link = link.model_copy(
                    update={"role": state.link_role_overrides[link.id]},
                )
            elif link.id in state.unapply_role_overrides:
                link = link.model_copy(
                    update={"role": state.unapply_role_overrides[link.id]},
                )
            result.append(link)
        return result

    async def record_mutation_event(
        self,
        event_type: str,
        target_id: str,
        payload: dict,
    ) -> None:
        """Record a mutation event for undo/staging support."""
        await self._execute(
            self.client.table("mutation_events").insert(
                {
                    "id": str(uuid.uuid4()),
                    "run_id": self.run_id,
                    "event_type": event_type,
                    "target_id": target_id,
                    "payload": payload,
                }
            )
        )
        self._invalidate_mutation_cache()

    async def get_or_create_project(self, name: str) -> tuple[Project, bool]:
        return await self.projects.get_or_create_project(name)

    async def list_projects_summary(
        self,
        include_hidden: bool = False,
    ) -> list[dict[str, Any]]:
        return await self.projects.list_projects_summary(include_hidden=include_hidden)

    async def list_projects(self, include_hidden: bool = False) -> list[Project]:
        return await self.projects.list_projects(include_hidden=include_hidden)

    async def get_project(self, project_id: str) -> Project | None:
        return await self.projects.get_project(project_id)

    async def update_project(
        self,
        project_id: str,
        *,
        name: str | None = None,
        hidden: bool | None = None,
    ) -> Project | None:
        return await self.projects.update_project(project_id, name=name, hidden=hidden)

    async def bulk_hide_projects(self, project_ids: Sequence[str]) -> int:
        return await self.projects.bulk_hide_projects(project_ids)

    async def update_run_hidden(self, run_id: str, hidden: bool) -> dict[str, Any] | None:
        return await self.runs.update_run_hidden(run_id, hidden)

    async def save_page(self, page: Page) -> None:
        return await self.pages.save_page(page)

    async def update_page_importance(self, page_id: str, importance: int) -> None:
        return await self.pages.update_page_importance(page_id, importance)

    async def update_page_content(self, page_id: str, new_content: str) -> None:
        return await self.pages.update_page_content(page_id, new_content)

    async def update_page_abstract(self, page_id: str, abstract: str) -> None:
        return await self.pages.update_page_abstract(page_id, abstract)

    async def update_page_task_shape(self, page_id: str, task_shape: dict | None) -> None:
        return await self.pages.update_page_task_shape(page_id, task_shape)

    async def workspace_coverage(self) -> dict[str, dict[str, int]]:
        return await self.pages.workspace_coverage()

    async def merge_page_extra(self, page_id: str, updates: dict) -> None:
        return await self.pages.merge_page_extra(page_id, updates)

    async def get_page(self, page_id: str) -> Page | None:
        return await self.pages.get_page(page_id)

    async def get_pages_by_ids(self, page_ids: Sequence[str]) -> dict[str, Page]:
        return await self.pages.get_pages_by_ids(page_ids)

    async def resolve_page_ids(self, page_ids: Sequence[str]) -> dict[str, str]:
        return await self.pages.resolve_page_ids(page_ids)

    async def resolve_page_id(self, page_id: str) -> str | None:
        return await self.pages.resolve_page_id(page_id)

    async def resolve_call_id(self, call_id: str) -> str | None:
        return await self.calls.resolve_call_id(call_id)

    async def resolve_link_id(self, link_id: str) -> str | None:
        return await self.pages.resolve_link_id(link_id)

    async def page_label(self, page_id: str) -> str:
        return await self.pages.page_label(page_id)

    async def get_pages_slim(self, active_only: bool = True) -> list[Page]:
        return await self.pages.get_pages_slim(active_only=active_only)

    async def get_pages(
        self,
        workspace: Workspace | None = None,
        page_type: PageType | None = None,
        active_only: bool = True,
    ) -> list[Page]:
        return await self.pages.get_pages(
            workspace=workspace, page_type=page_type, active_only=active_only
        )

    async def supersede_page(
        self,
        old_id: str,
        new_id: str,
        change_magnitude: int | None = None,
    ) -> None:
        return await self.pages.supersede_page(old_id, new_id, change_magnitude=change_magnitude)

    async def get_pages_paginated(
        self,
        workspace: Workspace | None = None,
        page_type: PageType | None = None,
        active_only: bool = True,
        search: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[Sequence[Page], int]:
        return await self.pages.get_pages_paginated(
            workspace=workspace,
            page_type=page_type,
            active_only=active_only,
            search=search,
            offset=offset,
            limit=limit,
        )

    async def resolve_supersession_chain(
        self,
        page_id: str,
        max_depth: int = 10,
    ) -> Page | None:
        return await self.pages.resolve_supersession_chain(page_id, max_depth=max_depth)

    async def resolve_supersession_chains(
        self,
        page_ids: Sequence[str],
        max_depth: int = 10,
    ) -> dict[str, Page]:
        return await self.pages.resolve_supersession_chains(page_ids, max_depth=max_depth)

    async def save_link(self, link: PageLink) -> None:
        return await self.links.save_link(link)

    async def _find_duplicate_link(self, link: PageLink) -> PageLink | None:
        return await self.links._find_duplicate_link(link)

    async def get_link(self, link_id: str) -> PageLink | None:
        return await self.links.get_link(link_id)

    async def get_links_to(self, page_id: str) -> list[PageLink]:
        return await self.links.get_links_to(page_id)

    async def get_view_for_question(self, question_id: str) -> Page | None:
        return await self.links.get_view_for_question(question_id)

    async def get_inlays_for_question(self, question_id: str) -> list[Page]:
        return await self.links.get_inlays_for_question(question_id)

    async def get_views_for_questions(
        self,
        question_ids: Sequence[str],
    ) -> dict[str, Page | None]:
        return await self.links.get_views_for_questions(question_ids)

    async def get_view_items(
        self,
        view_id: str,
        min_importance: int | None = None,
    ) -> list[tuple[Page, PageLink]]:
        return await self.links.get_view_items(view_id, min_importance=min_importance)

    async def get_links_from(self, page_id: str) -> list[PageLink]:
        return await self.links.get_links_from(page_id)

    async def get_links_from_many(
        self,
        page_ids: Sequence[str],
    ) -> dict[str, list[PageLink]]:
        return await self.links.get_links_from_many(page_ids)

    async def get_links_to_many(
        self,
        page_ids: Sequence[str],
    ) -> dict[str, list[PageLink]]:
        return await self.links.get_links_to_many(page_ids)

    async def get_considerations_for_question(
        self,
        question_id: str,
    ) -> list[tuple[Page, PageLink]]:
        return await self.links.get_considerations_for_question(question_id)

    async def get_considerations_for_questions(
        self,
        question_ids: Sequence[str],
    ) -> dict[str, list[tuple[Page, PageLink]]]:
        return await self.links.get_considerations_for_questions(question_ids)

    async def get_parent_question(self, question_id: str) -> Page | None:
        return await self.links.get_parent_question(question_id)

    async def get_child_questions(self, parent_id: str) -> list[Page]:
        return await self.links.get_child_questions(parent_id)

    async def get_child_questions_with_links(
        self,
        parent_id: str,
    ) -> list[tuple[Page, PageLink]]:
        return await self.links.get_child_questions_with_links(parent_id)

    async def get_judgements_for_question(self, question_id: str) -> list[Page]:
        return await self.links.get_judgements_for_question(question_id)

    async def get_judgements_for_questions(
        self,
        question_ids: Sequence[str],
    ) -> dict[str, list[Page]]:
        return await self.links.get_judgements_for_questions(question_ids)

    async def get_tension_verdicts_for_question(self, question_id: str) -> list[Page]:
        return await self.links.get_tension_verdicts_for_question(question_id)

    async def get_dependents(
        self,
        page_id: str,
    ) -> list[tuple[Page, PageLink]]:
        return await self.links.get_dependents(page_id)

    async def get_dependencies(
        self,
        page_id: str,
    ) -> list[tuple[Page, PageLink]]:
        return await self.links.get_dependencies(page_id)

    async def get_stale_dependencies(self) -> list[tuple[PageLink, int | None]]:
        return await self.links.get_stale_dependencies()

    async def get_dependency_counts(self) -> dict[str, int]:
        return await self.links.get_dependency_counts()

    async def _get_supersession_magnitude(self, page_id: str) -> int | None:
        return await self.links._get_supersession_magnitude(page_id)

    async def _get_supersession_magnitudes_many(
        self,
        page_ids: Sequence[str],
    ) -> dict[str, int | None]:
        return await self.links._get_supersession_magnitudes_many(page_ids)

    async def create_call(
        self,
        call_type: CallType,
        scope_page_id: str | None = None,
        parent_call_id: str | None = None,
        budget_allocated: int | None = None,
        workspace: Workspace = Workspace.RESEARCH,
        context_page_ids: Sequence[str] | None = None,
        call_id: str | None = None,
        sequence_id: str | None = None,
        sequence_position: int | None = None,
    ) -> Call:
        return await self.calls.create_call(
            call_type,
            scope_page_id=scope_page_id,
            parent_call_id=parent_call_id,
            budget_allocated=budget_allocated,
            workspace=workspace,
            context_page_ids=context_page_ids,
            call_id=call_id,
            sequence_id=sequence_id,
            sequence_position=sequence_position,
        )

    async def save_call(self, call: Call) -> None:
        return await self.calls.save_call(call)

    async def get_call(self, call_id: str) -> Call | None:
        return await self.calls.get_call(call_id)

    async def update_call_status(
        self,
        call_id: str,
        status: CallStatus,
        result_summary: str = "",
        call_params: dict | None = None,
        cost_usd: float | None = None,
    ) -> None:
        return await self.calls.update_call_status(
            call_id,
            status,
            result_summary=result_summary,
            call_params=call_params,
            cost_usd=cost_usd,
        )

    async def increment_call_budget_used(
        self,
        call_id: str,
        amount: int = 1,
    ) -> None:
        return await self.calls.increment_call_budget_used(call_id, amount=amount)

    async def init_budget(self, total: int) -> None:
        return await self.runs.init_budget(total)

    async def get_budget(self) -> tuple[int, int]:
        return await self.runs.get_budget()

    async def consume_budget(self, amount: int = 1) -> bool:
        return await self.runs.consume_budget(amount)

    async def add_budget(self, amount: int) -> None:
        return await self.runs.add_budget(amount)

    async def budget_remaining(self) -> int:
        return await self.runs.budget_remaining()

    async def get_links_between(
        self,
        from_page_id: str,
        to_page_id: str,
    ) -> list[PageLink]:
        return await self.links.get_links_between(from_page_id, to_page_id)

    async def get_all_links(
        self,
        page_ids: set[str] | None = None,
    ) -> list[PageLink]:
        return await self.links.get_all_links(page_ids)

    async def _get_links_for_pages(
        self,
        page_ids: set[str],
    ) -> list[PageLink]:
        return await self.links._get_links_for_pages(page_ids)

    async def delete_link(self, link_id: str) -> None:
        return await self.links.delete_link(link_id)

    async def update_link_role(self, link_id: str, role: LinkRole) -> None:
        return await self.links.update_link_role(link_id, role)

    async def get_last_find_considerations_info(
        self,
        question_id: str,
    ) -> tuple[str, int | None] | None:
        return await self.calls.get_last_find_considerations_info(question_id)

    async def get_call_counts_by_type(
        self,
        question_id: str,
    ) -> dict[str, int]:
        return await self.calls.get_call_counts_by_type(question_id)

    async def get_latest_scout_fruit(
        self,
        question_id: str,
    ) -> dict[str, int | None]:
        return await self.calls.get_latest_scout_fruit(question_id)

    async def get_ingest_history(self) -> dict[str, list[str]]:
        return await self.calls.get_ingest_history()

    async def save_call_trace(self, call_id: str, events: Sequence[dict]) -> None:
        return await self.calls.save_call_trace(call_id, events)

    async def get_call_trace(self, call_id: str) -> list[dict]:
        return await self.calls.get_call_trace(call_id)

    async def get_child_calls(self, parent_call_id: str) -> list[Call]:
        return await self.calls.get_child_calls(parent_call_id)

    async def create_call_sequence(
        self,
        parent_call_id: str | None,
        scope_question_id: str | None,
        position_in_batch: int = 0,
    ) -> CallSequence:
        return await self.calls.create_call_sequence(
            parent_call_id, scope_question_id, position_in_batch=position_in_batch
        )

    async def get_sequences_for_call(
        self,
        parent_call_id: str,
    ) -> Sequence[CallSequence]:
        return await self.calls.get_sequences_for_call(parent_call_id)

    async def get_calls_for_sequence(
        self,
        sequence_id: str,
    ) -> Sequence[Call]:
        return await self.calls.get_calls_for_sequence(sequence_id)

    async def get_root_calls_for_question(self, question_id: str) -> list[Call]:
        return await self.calls.get_root_calls_for_question(question_id)

    async def get_recent_calls_for_question(
        self,
        question_id: str,
        limit: int = 10,
    ) -> list[Call]:
        return await self.calls.get_recent_calls_for_question(question_id, limit=limit)

    async def save_page_rating(
        self,
        page_id: str,
        call_id: str,
        score: int,
        note: str = "",
    ) -> None:
        return await self.annotations.save_page_rating(page_id, call_id, score, note=note)

    async def save_page_flag(
        self,
        flag_type: str,
        call_id: str | None = None,
        note: str = "",
        page_id: str | None = None,
        page_id_a: str | None = None,
        page_id_b: str | None = None,
    ) -> None:
        return await self.annotations.save_page_flag(
            flag_type,
            call_id=call_id,
            note=note,
            page_id=page_id,
            page_id_a=page_id_a,
            page_id_b=page_id_b,
        )

    async def record_reputation_event(
        self,
        *,
        source: str,
        dimension: str,
        score: float,
        orchestrator: str | None = None,
        task_shape: dict | None = None,
        source_call_id: str | None = None,
        extra: dict | None = None,
    ) -> None:
        return await self.annotations.record_reputation_event(
            source=source,
            dimension=dimension,
            score=score,
            orchestrator=orchestrator,
            task_shape=task_shape,
            source_call_id=source_call_id,
            extra=extra,
        )

    async def get_reputation_events(
        self,
        *,
        run_id: str | None = None,
        source: str | None = None,
        dimension: str | None = None,
        orchestrator: str | None = None,
    ) -> list[ReputationEvent]:
        return await self.annotations.get_reputation_events(
            run_id=run_id,
            source=source,
            dimension=dimension,
            orchestrator=orchestrator,
        )

    async def get_reputation_summary(
        self,
        project_id: str,
        *,
        orchestrator: str | None = None,
        source: str | None = None,
        dimension: str | None = None,
    ) -> list[dict[str, Any]]:
        return await self.annotations.get_reputation_summary(
            project_id,
            orchestrator=orchestrator,
            source=source,
            dimension=dimension,
        )

    async def get_eval_summary_for_pages(
        self,
        page_ids: Sequence[str],
        dimensions: Sequence[str],
    ) -> dict[str, dict[str, "EvalSummary"]]:
        return await self.annotations.get_eval_summary_for_pages(page_ids, dimensions)

    async def get_eval_summary_for_calls(
        self,
        call_ids: Sequence[str],
        dimensions: Sequence[str],
    ) -> dict[str, dict[str, "EvalSummary"]]:
        return await self.annotations.get_eval_summary_for_calls(call_ids, dimensions)

    async def record_annotation(
        self,
        *,
        annotation_type: str,
        author_type: str,
        author_id: str,
        target_page_id: str | None = None,
        target_call_id: str | None = None,
        target_event_seq: int | None = None,
        span_start: int | None = None,
        span_end: int | None = None,
        category: str | None = None,
        note: str = "",
        payload: dict | None = None,
        extra: dict | None = None,
    ) -> AnnotationEvent:
        return await self.annotations.record_annotation(
            annotation_type=annotation_type,
            author_type=author_type,
            author_id=author_id,
            target_page_id=target_page_id,
            target_call_id=target_call_id,
            target_event_seq=target_event_seq,
            span_start=span_start,
            span_end=span_end,
            category=category,
            note=note,
            payload=payload,
            extra=extra,
        )

    async def get_annotations(
        self,
        *,
        target_page_id: str | None = None,
        target_call_id: str | None = None,
        author_type: str | None = None,
        annotation_type: str | None = None,
    ) -> list[AnnotationEvent]:
        return await self.annotations.get_annotations(
            target_page_id=target_page_id,
            target_call_id=target_call_id,
            author_type=author_type,
            annotation_type=annotation_type,
        )

    async def get_annotations_by_target_pages(
        self,
        page_ids: Sequence[str],
    ) -> dict[str, list[AnnotationEvent]]:
        return await self.annotations.get_annotations_by_target_pages(page_ids)

    async def save_epistemic_score(
        self,
        page_id: str,
        call_id: str,
        credence: int | None = None,
        robustness: int | None = None,
        reasoning: str = "",
        source_page_id: str | None = None,
    ) -> None:
        return await self.annotations.save_epistemic_score(
            page_id,
            call_id,
            credence=credence,
            robustness=robustness,
            reasoning=reasoning,
            source_page_id=source_page_id,
        )

    async def save_page_format_events(self, call_id: str, events: Sequence[dict[str, Any]]) -> None:
        return await self.annotations.save_page_format_events(call_id, events)

    async def get_page_format_events_for_run(self, run_id: str) -> Sequence[dict[str, Any]]:
        return await self.annotations.get_page_format_events_for_run(run_id)

    async def get_epistemic_score_source(
        self,
        page_id: str,
    ) -> tuple[dict[str, Any] | None, Page | None]:
        return await self.annotations.get_epistemic_score_source(page_id)

    async def get_latest_judgement_for_call(
        self,
        call_id: str,
    ) -> str | None:
        return await self.calls.get_latest_judgement_for_call(call_id)

    async def get_root_questions(
        self,
        workspace: Workspace = Workspace.RESEARCH,
        *,
        include_duplicates: bool = False,
    ) -> list[Page]:
        return await self.pages.get_root_questions(
            workspace=workspace, include_duplicates=include_duplicates
        )

    async def get_human_questions(
        self,
        workspace: Workspace = Workspace.RESEARCH,
    ) -> list[Page]:
        return await self.pages.get_human_questions(workspace=workspace)

    async def count_pages_for_question(self, question_id: str) -> dict:
        return await self.pages.count_pages_for_question(question_id)

    async def get_project_stats(self, project_id: str) -> dict[str, Any]:
        return await self.projects.get_project_stats(project_id)

    async def get_question_stats(self, question_id: str) -> dict[str, Any]:
        return await self.projects.get_question_stats(question_id)

    async def get_assess_staleness(
        self,
        question_ids: Sequence[str],
    ) -> dict[str, bool]:
        return await self.pages.get_assess_staleness(question_ids)

    async def count_pages_since(self, since: datetime) -> int:
        return await self.pages.count_pages_since(since)

    async def save_llm_exchange(
        self,
        call_id: str,
        phase: str,
        system_prompt: str | None,
        user_message: str | None,
        response_text: str | None,
        tool_calls: Sequence[dict] | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        error: str | None = None,
        duration_ms: int | None = None,
        round_num: int | None = None,
        cache_creation_input_tokens: int | None = None,
        cache_read_input_tokens: int | None = None,
        user_messages: Sequence[dict] | None = None,
    ) -> str:
        return await self.calls.save_llm_exchange(
            call_id,
            phase,
            system_prompt,
            user_message,
            response_text,
            tool_calls=tool_calls,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            error=error,
            duration_ms=duration_ms,
            round_num=round_num,
            cache_creation_input_tokens=cache_creation_input_tokens,
            cache_read_input_tokens=cache_read_input_tokens,
            user_messages=user_messages,
        )

    async def get_llm_exchanges(self, call_id: str) -> list[dict[str, Any]]:
        return await self.calls.get_llm_exchanges(call_id)

    async def get_llm_exchange(self, exchange_id: str) -> dict[str, Any] | None:
        return await self.calls.get_llm_exchange(exchange_id)

    async def get_call_rows_for_run(self, run_id: str) -> list[dict]:
        return await self.runs.get_call_rows_for_run(run_id)

    async def get_calls_for_run(self, run_id: str) -> list[Call]:
        return await self.runs.get_calls_for_run(run_id)

    async def get_run_question_id(self, run_id: str) -> str | None:
        return await self.runs.get_run_question_id(run_id)

    async def get_run_for_page(self, page_id: str) -> dict[str, Any] | None:
        return await self.runs.get_run_for_page(page_id)

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        return await self.runs.get_run(run_id)

    async def create_run(
        self,
        name: str,
        question_id: str | None,
        config: dict | None = None,
        orchestrator: str | None = None,
    ) -> None:
        return await self.runs.create_run(name, question_id, config, orchestrator)

    async def get_or_create_named_run(
        self,
        project_id: str,
        name: str,
        config: dict | None = None,
    ) -> str:
        return await self.runs.get_or_create_named_run(project_id, name, config)

    async def count_run_questions(self) -> int:
        return await self.runs.count_run_questions()

    async def get_run_questions_since(
        self,
        since: datetime,
    ) -> list[Page]:
        return await self.runs.get_run_questions_since(since)

    async def stage_run(self, run_id: str) -> None:
        """Retroactively stage a completed non-staged run.

        Flips the staged flag on the run's rows, then reverts direct
        mutations (supersessions, link deletions, role changes) so baseline
        readers see the pre-run state. The mutation events remain, so a
        staged reader replaying them will see the same view the run
        originally produced.
        """
        await self._execute(self.client.table("runs").update({"staged": True}).eq("id", run_id))
        for table in (
            "pages",
            "page_links",
            "page_ratings",
            "page_flags",
            "call_llm_exchanges",
            "page_format_events",
            "reputation_events",
            "annotation_events",
        ):
            await self._execute(
                self.client.table(table).update({"staged": True}).eq("run_id", run_id)
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
                await self._execute(self.client.table("page_links").upsert(restore_row))

            elif et == "change_link_role":
                old_role = payload.get("old_role")
                if not old_role:
                    log.warning(
                        "Cannot revert role change for link %s: no old_role in event payload",
                        tid,
                    )
                    continue
                link_rows = _rows(
                    await self._execute(
                        self.client.table("page_links").select("run_id").eq("id", tid)
                    )
                )
                if link_rows and link_rows[0].get("run_id") == run_id:
                    continue
                await self._execute(
                    self.client.table("page_links").update({"role": old_role}).eq("id", tid)
                )

            elif et == "update_page_content":
                if "old_content" not in payload:
                    log.warning(
                        "Cannot revert content update for page %s: no old_content in event payload",
                        tid,
                    )
                    continue
                page_rows = _rows(
                    await self._execute(self.client.table("pages").select("run_id").eq("id", tid))
                )
                if page_rows and page_rows[0].get("run_id") == run_id:
                    continue
                await self._execute(
                    self.client.table("pages")
                    .update({"content": payload["old_content"]})
                    .eq("id", tid)
                )

            elif et in ("set_credence", "set_robustness"):
                if "old_value" not in payload:
                    log.warning(
                        "Cannot revert %s for page %s: no old_value in event payload",
                        et,
                        tid,
                    )
                    continue
                page_rows = _rows(
                    await self._execute(self.client.table("pages").select("run_id").eq("id", tid))
                )
                if page_rows and page_rows[0].get("run_id") == run_id:
                    continue
                score_col = "credence" if et == "set_credence" else "robustness"
                reason_col = f"{score_col}_reasoning"
                await self._execute(
                    self.client.table("pages")
                    .update(
                        {
                            score_col: payload.get("old_value"),
                            reason_col: payload.get("old_reasoning"),
                        }
                    )
                    .eq("id", tid)
                )

    async def commit_staged_run(self, run_id: str) -> None:
        """Commit a staged run, making its effects visible to all readers.

        Flips the staged flag to false on the run's rows, then applies
        mutation events (supersessions, link deletions, role changes) that
        were recorded but never written directly to the database.
        """
        run_rows = _rows(
            await self._execute(self.client.table("runs").select("id, staged").eq("id", run_id))
        )
        if not run_rows:
            raise ValueError(f"Run {run_id} not found")
        if not run_rows[0].get("staged"):
            raise ValueError(f"Run {run_id} is not staged")

        await self._execute(self.client.table("runs").update({"staged": False}).eq("id", run_id))
        for table in (
            "pages",
            "page_links",
            "page_ratings",
            "page_flags",
            "call_llm_exchanges",
            "page_format_events",
            "reputation_events",
            "annotation_events",
        ):
            await self._execute(
                self.client.table(table).update({"staged": False}).eq("run_id", run_id)
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
                await self._execute(self.client.table("page_links").delete().eq("id", tid))

            elif et == "change_link_role":
                new_role = payload.get("new_role")
                if not new_role:
                    log.warning(
                        "Cannot apply role change for link %s: no new_role in event payload",
                        tid,
                    )
                    continue
                await self._execute(
                    self.client.table("page_links").update({"role": new_role}).eq("id", tid)
                )

            elif et == "update_page_content":
                if "new_content" not in payload:
                    log.warning(
                        "Cannot apply content update for page %s: no new_content in event payload",
                        tid,
                    )
                    continue
                await self._execute(
                    self.client.table("pages")
                    .update({"content": payload["new_content"]})
                    .eq("id", tid)
                )

            elif et in ("set_credence", "set_robustness"):
                if "value" not in payload:
                    log.warning(
                        "Cannot apply %s for page %s: no value in event payload",
                        et,
                        tid,
                    )
                    continue
                score_col = "credence" if et == "set_credence" else "robustness"
                reason_col = f"{score_col}_reasoning"
                await self._execute(
                    self.client.table("pages")
                    .update(
                        {
                            score_col: payload.get("value"),
                            reason_col: payload.get("reasoning"),
                        }
                    )
                    .eq("id", tid)
                )

    async def save_ab_eval_report(
        self,
        run_id_a: str,
        run_id_b: str,
        question_id_a: str,
        question_id_b: str,
        overall_assessment: str,
        dimension_reports: Sequence[dict[str, Any]],
        overall_assessment_call_id: str | None = None,
    ) -> str:
        return await self.runs.save_ab_eval_report(
            run_id_a,
            run_id_b,
            question_id_a,
            question_id_b,
            overall_assessment,
            dimension_reports,
            overall_assessment_call_id=overall_assessment_call_id,
        )

    async def list_ab_eval_reports(self) -> list[dict[str, Any]]:
        return await self.runs.list_ab_eval_reports()

    async def get_ab_eval_report(self, report_id: str) -> dict[str, Any] | None:
        return await self.runs.get_ab_eval_report(report_id)

    async def save_run_eval_report(
        self,
        run_id: str,
        question_id: str,
        overall_assessment: str,
        dimension_reports: Sequence[dict[str, Any]],
    ) -> str:
        return await self.runs.save_run_eval_report(
            run_id, question_id, overall_assessment, dimension_reports
        )

    async def list_run_eval_reports(self) -> list[dict[str, Any]]:
        return await self.runs.list_run_eval_reports()

    async def get_run_eval_report(self, report_id: str) -> dict[str, Any] | None:
        return await self.runs.get_run_eval_report(report_id)

    async def list_runs_for_project(self, project_id: str, limit: int = 50) -> list[dict[str, Any]]:
        return await self.runs.list_runs_for_project(project_id, limit=limit)

    async def delete_run_data(self, delete_project: bool = False) -> None:
        return await self.runs.delete_run_data(delete_project=delete_project)

    async def save_suggestion(self, suggestion: Suggestion) -> None:
        return await self.chat.save_suggestion(suggestion)

    async def get_pending_suggestions(
        self,
        target_page_id: str | None = None,
    ) -> list[Suggestion]:
        return await self.chat.get_pending_suggestions(target_page_id=target_page_id)

    async def get_suggestions(
        self,
        status: str = "pending",
        target_page_id: str | None = None,
    ) -> list[Suggestion]:
        return await self.chat.get_suggestions(status=status, target_page_id=target_page_id)

    async def get_suggestion(self, suggestion_id: str) -> Suggestion | None:
        return await self.chat.get_suggestion(suggestion_id)

    async def update_suggestion_status(
        self,
        suggestion_id: str,
        status: SuggestionStatus,
    ) -> None:
        return await self.chat.update_suggestion_status(suggestion_id, status)

    async def create_chat_conversation(
        self,
        project_id: str,
        question_id: str | None = None,
        title: str = "",
    ) -> ChatConversation:
        return await self.chat.create_chat_conversation(
            project_id, question_id=question_id, title=title
        )

    async def get_chat_conversation(self, conversation_id: str) -> ChatConversation | None:
        return await self.chat.get_chat_conversation(conversation_id)

    async def list_chat_conversations(
        self,
        project_id: str,
        limit: int = 50,
        offset: int = 0,
        question_id: str | None = None,
    ) -> Sequence[ChatConversation]:
        return await self.chat.list_chat_conversations(
            project_id, limit=limit, offset=offset, question_id=question_id
        )

    async def update_chat_conversation(
        self,
        conversation_id: str,
        title: str | None = None,
        touch: bool = False,
    ) -> None:
        return await self.chat.update_chat_conversation(conversation_id, title=title, touch=touch)

    async def soft_delete_chat_conversation(self, conversation_id: str) -> None:
        return await self.chat.soft_delete_chat_conversation(conversation_id)

    async def save_chat_message(
        self,
        conversation_id: str,
        role: ChatMessageRole,
        content: dict,
        seq: int | None = None,
        question_id: str | None = None,
    ) -> ChatMessage:
        return await self.chat.save_chat_message(
            conversation_id, role, content, seq=seq, question_id=question_id
        )

    async def _next_chat_message_seq(self, conversation_id: str) -> int:
        return await self.chat._next_chat_message_seq(conversation_id)

    async def list_chat_messages(
        self,
        conversation_id: str,
    ) -> Sequence[ChatMessage]:
        return await self.chat.list_chat_messages(conversation_id)

    async def branch_chat_conversation(
        self,
        source_conversation_id: str,
        at_seq: int,
        title: str | None = None,
    ) -> ChatConversation:
        return await self.chat.branch_chat_conversation(source_conversation_id, at_seq, title=title)
