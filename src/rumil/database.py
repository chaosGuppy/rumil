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


class EvalSummary:
    """Lightweight numeric summary of reputation events for one subject+dimension.

    Kept as a plain class (not a pydantic model) because it's purely a
    query-time aggregate — callers consume ``mean`` / ``count`` / ``latest``
    and do not persist instances.
    """

    __slots__ = ("count", "dimension", "latest", "mean")

    def __init__(self, *, dimension: str, mean: float, count: int, latest: float) -> None:
        self.dimension = dimension
        self.mean = mean
        self.count = count
        self.latest = latest

    def __repr__(self) -> str:
        return (
            f"EvalSummary(dimension={self.dimension!r}, mean={self.mean:.3f}, "
            f"count={self.count}, latest={self.latest:.3f})"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, EvalSummary):
            return NotImplemented
        return (
            self.dimension == other.dimension
            and self.count == other.count
            and self.mean == other.mean
            and self.latest == other.latest
        )


def _aggregate_eval_rows_by_subject(
    rows: Sequence[dict[str, Any]],
    *,
    subject_key: str,
) -> dict[str, dict[str, EvalSummary]]:
    """Group reputation_events rows by (subject_id, dimension).

    Rows must carry ``extra[subject_key]`` — otherwise they're skipped,
    not an error (the index filter already constrains to rows with
    ``extra ? 'subject_*_id'``).
    """
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        extra = r.get("extra") or {}
        subject = extra.get(subject_key)
        if not subject:
            continue
        dim = r["dimension"]
        score = float(r["score"])
        created_at = r.get("created_at") or ""
        key = (subject, dim)
        bucket = buckets.get(key)
        if bucket is None:
            buckets[key] = {
                "sum_score": score,
                "count": 1,
                "latest_score": score,
                "latest_at": created_at,
            }
        else:
            bucket["sum_score"] += score
            bucket["count"] += 1
            if created_at > bucket["latest_at"]:
                bucket["latest_at"] = created_at
                bucket["latest_score"] = score

    result: dict[str, dict[str, EvalSummary]] = {}
    for (subject, dim), b in buckets.items():
        n = b["count"]
        summary = EvalSummary(
            dimension=dim,
            mean=b["sum_score"] / n,
            count=n,
            latest=b["latest_score"],
        )
        result.setdefault(subject, {})[dim] = summary
    return result


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
        from rumil.db.call_store import CallStore
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
        log.debug(
            "save_page: id=%s, type=%s, headline=%s",
            page.id[:8],
            page.page_type.value,
            page.headline[:60],
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
                    "run_id": self.run_id,
                    "staged": self.staged,
                    "abstract": page.abstract,
                    "task_shape": page.task_shape,
                }
            )
        )

    async def update_page_importance(self, page_id: str, importance: int) -> None:
        """Update the importance level on a page."""
        await self._execute(
            self.client.table("pages").update({"importance": importance}).eq("id", page_id)
        )

    async def update_page_content(self, page_id: str, new_content: str) -> None:
        """Update a page's content field with mutation event recording."""
        page = await self.get_page(page_id)
        if not page:
            raise ValueError(f"update_page_content: page {page_id} not found")
        await self.record_mutation_event(
            "update_page_content",
            page_id,
            {"old_content": page.content, "new_content": new_content},
        )
        if not self.staged:
            await self._execute(
                self.client.table("pages").update({"content": new_content}).eq("id", page_id)
            )

    async def update_page_abstract(self, page_id: str, abstract: str) -> None:
        await self._execute(
            self.client.table("pages").update({"abstract": abstract}).eq("id", page_id)
        )

    async def update_page_task_shape(self, page_id: str, task_shape: dict | None) -> None:
        """Set the task_shape JSONB payload on a page.

        Task-shape is metadata attached only to questions (v1 taxonomy).
        Non-question pages always store NULL.
        """
        await self._execute(
            self.client.table("pages").update({"task_shape": task_shape}).eq("id", page_id)
        )

    async def workspace_coverage(self) -> dict[str, dict[str, int]]:
        """Aggregate task_shape tag values across all question pages in the project.

        Returns a mapping ``{dimension: {value: count}}`` for every dimension
        that appears in any tagged question. Untagged questions contribute
        nothing. Used to report distribution of deliverable_shape /
        source_posture across a project.
        """
        query = (
            self.client.table("pages")
            .select("task_shape")
            .eq("page_type", PageType.QUESTION.value)
            .not_.is_("task_shape", "null")
        )
        if self.project_id:
            query = query.eq("project_id", self.project_id)
        query = self._staged_filter(query)
        rows = _rows(await self._execute(query))
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

        Reads the current ``extra`` (if any), shallow-merges ``updates`` into
        it, and writes the result back. Caller-provided keys overwrite existing
        ones. No mutation event is recorded — ``extra`` is append-only
        metadata that is not part of the staged-runs mutation surface.
        """
        page = await self.get_page(page_id)
        if not page:
            raise ValueError(f"merge_page_extra: page {page_id} not found")
        merged = {**(page.extra or {}), **updates}
        await self._execute(self.client.table("pages").update({"extra": merged}).eq("id", page_id))

    async def get_page(self, page_id: str) -> Page | None:
        query = self.client.table("pages").select("*").eq("id", page_id)
        return await self.overlay.read_page_opt(query)

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
            for page in await self.overlay.read_pages(query):
                result[page.id] = page
        return result

    async def resolve_page_ids(self, page_ids: Sequence[str]) -> dict[str, str]:
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
                await self._execute(self.client.table("pages").select("id").or_(or_clause))
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
        """Resolve a page ID to a full UUID. Handles both full UUIDs and
        8-char short IDs. Returns the full UUID if found, or None."""
        if not page_id:
            log.debug("resolve_page_id: empty page_id")
            return None
        # Try exact match first
        rows = _rows(await self._execute(self.client.table("pages").select("id").eq("id", page_id)))
        if rows:
            log.debug("resolve_page_id: exact match for %s", page_id[:8])
            return rows[0]["id"]
        # Try prefix match for short IDs
        if len(page_id) <= 8:
            rows = _rows(
                await self._execute(
                    self.client.table("pages").select("id").like("id", f"{page_id}%")
                )
            )
            if len(rows) == 1:
                log.debug(
                    "resolve_page_id: prefix match %s -> %s",
                    page_id,
                    rows[0]["id"][:8],
                )
                return rows[0]["id"]
            if len(rows) > 1:
                log.warning(
                    "Ambiguous short ID '%s' matches %d pages",
                    page_id,
                    len(rows),
                )
            else:
                log.debug("resolve_page_id: no prefix match for %s", page_id)
            return None
        if page_id.startswith("http"):
            rows = _rows(
                await self._execute(
                    self.client.table("pages").select("id").eq("extra->>url", page_id)
                )
            )
            if len(rows) == 1:
                log.debug(
                    "resolve_page_id: URL match %s -> %s",
                    page_id,
                    rows[0]["id"][:8],
                )
                return rows[0]["id"]
            if len(rows) > 1:
                log.debug(
                    "resolve_page_id: URL match %s -> %s (first of %d)",
                    page_id,
                    rows[0]["id"][:8],
                    len(rows),
                )
                return rows[0]["id"]
        log.debug("resolve_page_id: no match for %s", page_id[:8])
        return None

    async def resolve_call_id(self, call_id: str) -> str | None:
        return await self.calls.resolve_call_id(call_id)

    async def resolve_link_id(self, link_id: str) -> str | None:
        """Resolve a link ID to a full UUID. Handles both full UUIDs and
        8-char short IDs. Returns the full UUID if found, or None."""
        if not link_id:
            return None
        rows = _rows(
            await self._execute(self.client.table("page_links").select("id").eq("id", link_id))
        )
        if rows:
            return rows[0]["id"]
        if len(link_id) <= 8:
            rows = _rows(
                await self._execute(
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
        if self.project_id:
            query = query.eq("project_id", self.project_id)
        if active_only:
            query = query.eq("is_superseded", False)
        query = self._staged_filter(query)
        pages = [
            _row_to_page(r)
            for r in _rows(await self._execute(query.order("created_at", desc=True).limit(10000)))
        ]
        pages = await self._apply_page_events(pages)
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
            for r in _rows(await self._execute(query.order("created_at", desc=True).limit(10000)))
        ]
        pages = await self._apply_page_events(pages)
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
            "supersede_page",
            old_id,
            payload,
        )

        if not self.staged:
            await self._execute(
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
        if self.project_id:
            query = query.eq("project_id", self.project_id)
        if workspace:
            query = query.eq("workspace", workspace.value)
        if page_type:
            query = query.eq("page_type", page_type.value)
        if active_only:
            query = query.eq("is_superseded", False)
        if search:
            query = query.or_(f"headline.ilike.%{search}%,content.ilike.%{search}%")
        query = self._staged_filter(query)
        query = query.order(
            "is_human_created",
            desc=True,
        ).order("created_at", desc=True)
        end = offset + limit - 1
        result = await self._execute(query.range(offset, end))
        total = result.count or 0
        pages = [_row_to_page(r) for r in _rows(result)]
        pages = await self._apply_page_events(pages)
        if active_only:
            pages = [p for p in pages if p.is_active()]
        return pages, total

    async def resolve_supersession_chain(
        self,
        page_id: str,
        max_depth: int = 10,
    ) -> Page | None:
        """Follow superseded_by links from *page_id* to the final active page.

        Returns the end-of-chain (non-superseded) page, or ``None`` if the
        chain is broken (missing page) or exceeds *max_depth*.

        Delegates to the batched ``resolve_supersession_chains`` so the
        cost is dominated by level-wise batched fetches rather than one
        DB round trip per hop. Cycles terminate via the plural's
        depth bound.

        Note on max_depth: the singular historically counted *fetches*
        (``max_depth`` page fetches -> chains of length <= max_depth).
        The plural counts iterations *after* an initial fetch, so we
        pass ``max_depth - 1`` to preserve the singular's bound.
        """
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

    async def save_link(self, link: PageLink) -> None:
        log.debug(
            "save_link: %s -> %s, type=%s",
            link.from_page_id[:8],
            link.to_page_id[:8],
            link.link_type.value,
        )
        if get_settings().dedupe_page_links:
            existing = await self._find_duplicate_link(link)
            # Only skip when the duplicate is a *different* row — otherwise
            # we'd block the legitimate case of re-saving an existing link
            # to update its importance/section/etc.
            if existing is not None and existing.id != link.id:
                log.debug(
                    "save_link: dedup, existing link %s matches (from=%s to=%s type=%s)",
                    existing.id,
                    link.from_page_id[:8],
                    link.to_page_id[:8],
                    link.link_type.value,
                )
                return
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
                    "importance": link.importance,
                    "section": link.section,
                    "position": link.position,
                    "impact_on_parent_question": link.impact_on_parent_question,
                    "created_at": link.created_at.isoformat(),
                    "run_id": self.run_id,
                    "staged": self.staged,
                }
            )
        )

    async def _find_duplicate_link(self, link: PageLink) -> PageLink | None:
        """Return an existing link with the same (from, to, link_type) if one is
        already visible to this DB handle, else None.

        Respects staged-run visibility: a staged run sees baseline + own-run
        rows; a baseline run sees only baseline. This means staged and
        baseline dedup within their own views independently (a staged run
        can add the "same" link that baseline already has, but will see
        baseline's copy via the staged filter and skip — which is the
        correct behavior since dedup applies per visible view).
        """
        query = (
            self.client.table("page_links")
            .select("*")
            .eq("from_page_id", link.from_page_id)
            .eq("to_page_id", link.to_page_id)
            .eq("link_type", link.link_type.value)
            .limit(1)
        )
        query = self._staged_filter(query)
        rows = _rows(await self._execute(query))
        if not rows:
            return None
        applied = await self._apply_link_events([_row_to_link(rows[0])])
        return applied[0] if applied else None

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

    async def get_view_for_question(self, question_id: str) -> Page | None:
        """Find the active (non-superseded) View page for a question."""
        query = (
            self.client.table("page_links")
            .select(_LINK_COLUMNS)
            .eq("to_page_id", question_id)
            .eq("link_type", LinkType.VIEW_OF.value)
        )
        query = self._staged_filter(query)
        rows = _rows(await self._execute(query))
        links = await self._apply_link_events([_row_to_link(r) for r in rows])
        if not links:
            return None
        view_ids = [link.from_page_id for link in links]
        pages = await self.get_pages_by_ids(view_ids)
        for view_id in view_ids:
            page = pages.get(view_id)
            if page and not page.is_superseded:
                return page
        return None

    async def get_inlays_for_question(self, question_id: str) -> list[Page]:
        """Return active (non-superseded) INLAY pages bound to a question.

        Inlays are model-authored UI fragments that replace the stock
        content area for a question. See planning/inlay-ui.md. Issues two
        round trips (links + pages) regardless of how many inlays the
        question has.
        """
        query = (
            self.client.table("page_links")
            .select(_LINK_COLUMNS)
            .eq("to_page_id", question_id)
            .eq("link_type", LinkType.INLAY_OF.value)
        )
        query = self._staged_filter(query)
        rows = _rows(await self._execute(query))
        links = await self._apply_link_events([_row_to_link(r) for r in rows])
        if not links:
            return []
        inlay_ids = [link.from_page_id for link in links]
        pages = await self.get_pages_by_ids(inlay_ids)
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
        """Bulk-fetch the active (non-superseded) View page for many questions.

        Returns {question_id: view_page_or_None}. Issues two batched queries
        (links + pages) regardless of input size.
        """
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
        pages = await self.get_pages_by_ids(list(dict.fromkeys(view_from_ids)))
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
        """Get VIEW_ITEM pages linked to a View, with their link metadata.

        Returns (page, link) tuples sorted by section order then position.
        If *min_importance* is set, only items with importance >= that value
        are returned.  Items with importance=NULL (unscored proposals) are
        excluded when a minimum is specified.
        """
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
        pages_by_id = await self.get_pages_by_ids(item_ids)

        view_page = await self.get_page(view_id)
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
        return await self.overlay.read_links(query)

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
                query = self._staged_filter(query)
                rows = _rows(await self._execute(query.range(offset, offset + page_size - 1)))
                all_links.extend(_row_to_link(r) for r in rows)
                if len(rows) < page_size:
                    break
                offset += page_size
        applied = await self._apply_link_events(all_links)
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
                query = self._staged_filter(query)
                rows = _rows(await self._execute(query.range(offset, offset + page_size - 1)))
                all_links.extend(_row_to_link(r) for r in rows)
                if len(rows) < page_size:
                    break
                offset += page_size
        applied = await self._apply_link_events(all_links)
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
        pages = await self.get_pages_by_ids([l.from_page_id for l in consideration_links])
        return [
            (pages[l.from_page_id], l)
            for l in consideration_links
            if l.from_page_id in pages and pages[l.from_page_id].is_active()
        ]

    async def get_considerations_for_questions(
        self,
        question_ids: Sequence[str],
    ) -> dict[str, list[tuple[Page, PageLink]]]:
        """Bulk-fetch considerations for many questions. Returns {question_id: [(claim, link)]}."""
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
        pages = await self.get_pages_by_ids(page_ids)
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
        pages = await self.get_pages_by_ids([l.to_page_id for l in child_links])
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
        pages = await self.get_pages_by_ids([l.to_page_id for l in child_links])
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
        pages = await self.get_pages_by_ids([l.from_page_id for l in judgement_links])
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
        """Bulk-fetch active judgements for many questions. Returns {question_id: [judgements]}.

        Issues two batched queries (links + pages) regardless of input size.
        """
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
                query = self._staged_filter(query)
                rows = _rows(await self._execute(query.range(offset, offset + page_size - 1)))
                all_links.extend(_row_to_link(r) for r in rows)
                if len(rows) < page_size:
                    break
                offset += page_size
        applied = await self._apply_link_events(all_links)
        from_ids = list({l.from_page_id for l in applied})
        pages = await self.get_pages_by_ids(from_ids)
        for link in applied:
            page = pages.get(link.from_page_id)
            if page is not None and page.is_active() and page.page_type == PageType.JUDGEMENT:
                result.setdefault(link.to_page_id, []).append(page)
        return result

    async def get_tension_verdicts_for_question(self, question_id: str) -> list[Page]:
        """Return active TensionVerdict judgement pages for a question.

        TensionVerdicts are JUDGEMENT pages tagged with ``extra.tension_verdict``
        and ``extra.tension_pair.question_id`` — produced by ExploreTension
        calls. We filter by the question_id embedded in ``extra.tension_pair``
        rather than by a graph link, since verdicts link to the two claims
        (RELATED), not to the question.
        """
        query = (
            self.client.table("pages")
            .select("*")
            .eq("page_type", PageType.JUDGEMENT.value)
            .eq("is_superseded", False)
            .eq("extra->tension_pair->>question_id", question_id)
            .not_.is_("extra->tension_verdict", "null")
        )
        if self.project_id:
            query = query.eq("project_id", self.project_id)
        query = self._staged_filter(query)
        rows = _rows(await self._execute(query.order("created_at", desc=True)))
        pages = [_row_to_page(r) for r in rows]
        pages = await self._apply_page_events(pages)
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
        pages = await self.get_pages_by_ids([l.from_page_id for l in dep_links])
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
        pages = await self.get_pages_by_ids([l.to_page_id for l in dep_links])
        return [(pages[l.to_page_id], l) for l in dep_links if l.to_page_id in pages]

    async def get_stale_dependencies(self) -> list[tuple[PageLink, int | None]]:
        """Return DEPENDS_ON links where the dependency has been superseded.

        Returns (link, change_magnitude) pairs. change_magnitude comes from
        the supersession mutation event if available, otherwise None.

        Issues O(N_links / page_size) round trips to page through the
        depends_on table, plus one batched lookup each for target pages and
        supersession magnitudes — constant in the number of *stale* deps.
        """
        page_size = 1000
        offset = 0
        raw_rows: list[dict] = []
        while True:
            query = self.client.table("page_links").select("*").eq("link_type", "depends_on")
            query = self._staged_filter(query)
            rows = _rows(await self._execute(query.range(offset, offset + page_size - 1)))
            raw_rows.extend(rows)
            if len(rows) < page_size:
                break
            offset += page_size
        links = await self._apply_link_events([_row_to_link(r) for r in raw_rows])
        if not links:
            return []

        target_ids = list({l.to_page_id for l in links})
        pages_by_id = await self.get_pages_by_ids(target_ids)
        superseded_ids = [pid for pid, page in pages_by_id.items() if page.is_superseded]
        magnitudes = await self._get_supersession_magnitudes_many(superseded_ids)

        stale: list[tuple[PageLink, int | None]] = []
        for link in links:
            dep_page = pages_by_id.get(link.to_page_id)
            if dep_page and dep_page.is_superseded:
                stale.append((link, magnitudes.get(dep_page.id)))
        return stale

    async def get_dependency_counts(self) -> dict[str, int]:
        """Return a map from page_id to how many pages depend on it, within the current project.

        Scopes by intersecting link endpoints with the project's page IDs.
        `page_links` has no `project_id` column, so we resolve project membership
        via `pages`.
        """
        project_page_ids: set[str] | None = None
        if self.project_id:
            project_page_ids = set()
            offset = 0
            page_size = 1000
            while True:
                pages_query = (
                    self.client.table("pages").select("id").eq("project_id", self.project_id)
                )
                pages_query = self._staged_filter(pages_query)
                rows = _rows(await self._execute(pages_query.range(offset, offset + page_size - 1)))
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
            query = self._staged_filter(query)
            rows = _rows(await self._execute(query.range(offset, offset + page_size - 1)))
            raw_link_rows.extend(rows)
            if len(rows) < page_size:
                break
            offset += page_size
        links = await self._apply_link_events([_row_to_link(r) for r in raw_link_rows])

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
        """Look up change_magnitude for many superseded pages in one query.

        Returns a dict mapping page_id to the most-recent supersede_page
        event's change_magnitude (or None if the event exists but carries
        no magnitude). Pages without any supersede_page event are absent
        from the result.
        """
        if not page_ids:
            return {}
        query = (
            self.client.table("mutation_events")
            .select("target_id, payload, created_at")
            .in_("target_id", list(set(page_ids)))
            .eq("event_type", "supersede_page")
            .order("created_at", desc=True)
        )
        rows = _rows(await self._execute(query))
        # Rows are ordered newest-first; keep only the first per target_id.
        result: dict[str, int | None] = {}
        for row in rows:
            target = row["target_id"]
            if target in result:
                continue
            payload = row.get("payload") or {}
            result[target] = payload.get("change_magnitude")
        return result

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
        self,
        page_ids: set[str] | None = None,
    ) -> list[PageLink]:
        """Bulk-fetch links, scoped to a set of page IDs if provided.

        When *page_ids* is given, only links where at least one endpoint is in
        the set are returned. This avoids fetching every link in the DB when the
        caller already knows which pages matter.
        """
        if page_ids is not None:
            return await self._get_links_for_pages(page_ids)
        page_size = 2000
        if self.project_id:
            page_ids_query = self._staged_filter(
                self.client.table("pages").select("id").eq("project_id", self.project_id)
            )
            page_ids_rows = _rows(await self._execute(page_ids_query.limit(50000)))
            proj_page_ids = {r["id"] for r in page_ids_rows}
            all_rows: list[dict[str, Any]] = []
            offset = 0
            while True:
                query = self.client.table("page_links").select(_LINK_COLUMNS)
                query = self._staged_filter(query)
                rows = _rows(await self._execute(query.range(offset, offset + page_size - 1)))
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
                query = self._staged_filter(query)
                rows = _rows(await self._execute(query.range(offset, offset + page_size - 1)))
                all_rows.extend(rows)
                if len(rows) < page_size:
                    break
                offset += page_size
            links = [_row_to_link(r) for r in all_rows]
        return await self._apply_link_events(links)

    async def _get_links_for_pages(
        self,
        page_ids: set[str],
    ) -> list[PageLink]:
        """Fetch links where at least one endpoint is in *page_ids*.

        Batches into chunks to stay within URL-length limits and paginates
        within each batch to avoid PostgREST response-size failures.
        """
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
                    query = self._staged_filter(query)
                    rows = _rows(await self._execute(query.range(offset, offset + page_size - 1)))
                    for r in rows:
                        link = _row_to_link(r)
                        all_links[link.id] = link
                    if len(rows) < page_size:
                        break
                    offset += page_size
        return await self._apply_link_events(list(all_links.values()))

    async def delete_link(self, link_id: str) -> None:
        """Delete a page link by ID."""
        rows = _rows(
            await self._execute(
                self._staged_filter(self.client.table("page_links").select("*").eq("id", link_id))
            )
        )
        link_snapshot = rows[0] if rows else {}
        await self.record_mutation_event("delete_link", link_id, link_snapshot)
        if not self.staged:
            await self._execute(self.client.table("page_links").delete().eq("id", link_id))

    async def update_link_role(self, link_id: str, role: LinkRole) -> None:
        """Update a link's role."""
        link = await self.get_link(link_id)
        old_role = link.role.value if link else None
        await self.record_mutation_event(
            "change_link_role",
            link_id,
            {"new_role": role.value, "old_role": old_role},
        )
        if not self.staged:
            await self._execute(
                self.client.table("page_links").update({"role": role.value}).eq("id", link_id)
            )

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
        await self._execute(
            self.client.table("page_ratings").insert(
                {
                    "id": str(uuid.uuid4()),
                    "page_id": page_id,
                    "call_id": call_id,
                    "score": score,
                    "note": note,
                    "created_at": datetime.now(UTC).isoformat(),
                    "run_id": self.run_id,
                    "staged": self.staged,
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
                    "created_at": datetime.now(UTC).isoformat(),
                    "run_id": self.run_id,
                    "staged": self.staged,
                }
            )
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
        """Append a raw reputation signal for this run.

        Writes staged=self.staged and run_id=self.run_id so staged runs are
        isolated from baseline readers (see "Staged Runs and the Mutation
        Log" in CLAUDE.md). Never aggregate or normalize at this layer —
        callers keep each (source, dimension) raw.
        """
        row: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "run_id": self.run_id,
            "project_id": self.project_id,
            "source": source,
            "dimension": dimension,
            "score": score,
            "orchestrator": orchestrator,
            "task_shape": task_shape,
            "source_call_id": source_call_id,
            "extra": extra or {},
            "staged": self.staged,
            "created_at": datetime.now(UTC).isoformat(),
        }
        await self._execute(self.client.table("reputation_events").insert(row))

    async def get_reputation_events(
        self,
        *,
        run_id: str | None = None,
        source: str | None = None,
        dimension: str | None = None,
        orchestrator: str | None = None,
    ) -> list[ReputationEvent]:
        """Fetch reputation events, respecting the staged-visibility rule.

        Staged DBs see baseline (staged=false) events plus their own
        run_id's events. Non-staged DBs see only baseline events.
        """
        query = self.client.table("reputation_events").select("*")
        if self.project_id:
            query = query.eq("project_id", self.project_id)
        if run_id is not None:
            query = query.eq("run_id", run_id)
        if source is not None:
            query = query.eq("source", source)
        if dimension is not None:
            query = query.eq("dimension", dimension)
        if orchestrator is not None:
            query = query.eq("orchestrator", orchestrator)
        query = self._staged_filter(query)
        rows = _rows(await self._execute(query))
        return [
            ReputationEvent(
                id=r["id"],
                run_id=r["run_id"],
                project_id=r["project_id"],
                source=r["source"],
                dimension=r["dimension"],
                score=r["score"],
                orchestrator=r.get("orchestrator"),
                task_shape=r.get("task_shape"),
                source_call_id=r.get("source_call_id"),
                extra=r.get("extra") or {},
                staged=r.get("staged", False),
                created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in rows
        ]

    async def get_reputation_summary(
        self,
        project_id: str,
        *,
        orchestrator: str | None = None,
        source: str | None = None,
        dimension: str | None = None,
    ) -> list[dict[str, Any]]:
        """Group reputation events by (source, dimension, orchestrator).

        Returns a list of dicts with keys: source, dimension, orchestrator,
        n_events, mean_score, min_score, max_score, latest_at. Sources are
        never collapsed — each (source, dimension, orchestrator) triple is a
        separate bucket. Respects staging via the same visibility rule as
        ``get_reputation_events``.

        Grouping happens in Python over the filtered event set. This is
        simple and sufficient for dashboard-scale event counts; a SQL-level
        aggregate would need a new RPC that reproduces the staged-visibility
        logic (see "Staged Runs and the Mutation Log" in CLAUDE.md).
        """
        query = self.client.table("reputation_events").select("*").eq("project_id", project_id)
        if orchestrator is not None:
            query = query.eq("orchestrator", orchestrator)
        if source is not None:
            query = query.eq("source", source)
        if dimension is not None:
            query = query.eq("dimension", dimension)
        query = self._staged_filter(query)
        rows = _rows(await self._execute(query))

        buckets: dict[tuple[str, str, str | None], dict[str, Any]] = {}
        for r in rows:
            key = (r["source"], r["dimension"], r.get("orchestrator"))
            score = float(r["score"])
            created_at = r["created_at"]
            bucket = buckets.get(key)
            if bucket is None:
                buckets[key] = {
                    "source": r["source"],
                    "dimension": r["dimension"],
                    "orchestrator": r.get("orchestrator"),
                    "n_events": 1,
                    "sum_score": score,
                    "min_score": score,
                    "max_score": score,
                    "latest_at": created_at,
                }
            else:
                bucket["n_events"] += 1
                bucket["sum_score"] += score
                bucket["min_score"] = min(bucket["min_score"], score)
                bucket["max_score"] = max(bucket["max_score"], score)
                if created_at > bucket["latest_at"]:
                    bucket["latest_at"] = created_at

        result: list[dict[str, Any]] = []
        for b in buckets.values():
            n = b["n_events"]
            result.append(
                {
                    "source": b["source"],
                    "dimension": b["dimension"],
                    "orchestrator": b["orchestrator"],
                    "n_events": n,
                    "mean_score": b["sum_score"] / n,
                    "min_score": b["min_score"],
                    "max_score": b["max_score"],
                    "latest_at": b["latest_at"],
                }
            )
        result.sort(key=lambda b: (b["source"], b["dimension"], b["orchestrator"] or ""))
        return result

    async def get_eval_summary_for_pages(
        self,
        page_ids: Sequence[str],
        dimensions: Sequence[str],
    ) -> dict[str, dict[str, "EvalSummary"]]:
        """Batched eval summary per page per dimension.

        Returns ``{page_id: {dimension: EvalSummary}}``. Issues one query
        that filters by ``extra->>'subject_page_id' IN (...)`` and the
        requested dimensions. Uses the ``idx_rep_subject_page_id`` partial
        expression index from the ``eval_feedback_indexes`` migration.

        Respects the staged-visibility rule — filters project, applies
        ``_staged_filter``. Pages with no events for a given dimension are
        absent from that page's inner dict. Pages with no events at all
        are absent from the outer dict.
        """
        if not page_ids or not dimensions:
            return {}
        query = (
            self.client.table("reputation_events")
            .select("dimension,score,created_at,extra")
            .in_("extra->>subject_page_id", list(page_ids))
            .in_("dimension", list(dimensions))
        )
        if self.project_id:
            query = query.eq("project_id", self.project_id)
        query = self._staged_filter(query)
        rows = _rows(await self._execute(query))
        return _aggregate_eval_rows_by_subject(rows, subject_key="subject_page_id")

    async def get_eval_summary_for_calls(
        self,
        call_ids: Sequence[str],
        dimensions: Sequence[str],
    ) -> dict[str, dict[str, "EvalSummary"]]:
        """Batched eval summary per call per dimension.

        Parallel to ``get_eval_summary_for_pages`` but keyed on
        ``extra->>'subject_call_id'``. Used by the confusion-scan
        surfacing path.
        """
        if not call_ids or not dimensions:
            return {}
        query = (
            self.client.table("reputation_events")
            .select("dimension,score,created_at,extra")
            .in_("extra->>subject_call_id", list(call_ids))
            .in_("dimension", list(dimensions))
        )
        if self.project_id:
            query = query.eq("project_id", self.project_id)
        query = self._staged_filter(query)
        rows = _rows(await self._execute(query))
        return _aggregate_eval_rows_by_subject(rows, subject_key="subject_call_id")

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
        """Append a raw annotation signal.

        Mirrors ``record_reputation_event``: ``staged=self.staged`` and
        ``run_id=self.run_id`` at write time so staged runs are isolated from
        baseline readers (see "Staged Runs and the Mutation Log" in
        CLAUDE.md). Never aggregate or collapse at this layer — consumers
        group at query time.

        Returns the constructed ``AnnotationEvent`` so callers can inspect
        the ``id`` (useful for mirroring into ``reputation_events`` or
        returning from an HTTP handler).
        """
        ev = AnnotationEvent(
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
            payload=payload or {},
            extra=extra or {},
            run_id=self.run_id,
            project_id=self.project_id,
            staged=self.staged,
        )
        row: dict[str, Any] = {
            "id": ev.id,
            "project_id": ev.project_id,
            "run_id": ev.run_id,
            "annotation_type": ev.annotation_type,
            "author_type": ev.author_type,
            "author_id": ev.author_id,
            "target_page_id": ev.target_page_id,
            "target_call_id": ev.target_call_id,
            "target_event_seq": ev.target_event_seq,
            "span_start": ev.span_start,
            "span_end": ev.span_end,
            "category": ev.category,
            "note": ev.note,
            "payload": ev.payload,
            "extra": ev.extra,
            "staged": ev.staged,
            "created_at": ev.created_at.isoformat(),
        }
        await self._execute(self.client.table("annotation_events").insert(row))
        return ev

    async def get_annotations(
        self,
        *,
        target_page_id: str | None = None,
        target_call_id: str | None = None,
        author_type: str | None = None,
        annotation_type: str | None = None,
    ) -> list[AnnotationEvent]:
        """Fetch annotations, respecting the staged-visibility rule.

        Staged DBs see baseline (staged=false) rows plus their own run_id
        rows. Non-staged DBs see only baseline rows. Filters compose.
        """
        query = self.client.table("annotation_events").select("*")
        if self.project_id:
            query = query.eq("project_id", self.project_id)
        if target_page_id is not None:
            query = query.eq("target_page_id", target_page_id)
        if target_call_id is not None:
            query = query.eq("target_call_id", target_call_id)
        if author_type is not None:
            query = query.eq("author_type", author_type)
        if annotation_type is not None:
            query = query.eq("annotation_type", annotation_type)
        query = self._staged_filter(query)
        rows = _rows(await self._execute(query))
        return [_row_to_annotation_event(r) for r in rows]

    async def get_annotations_by_target_pages(
        self,
        page_ids: Sequence[str],
    ) -> dict[str, list[AnnotationEvent]]:
        """Batched annotation fetch: one query for many target pages.

        Returns a dict keyed by every input page_id — pages with no matching
        annotations map to an empty list. Respects the staged-visibility
        rule via ``_staged_filter`` (same semantics as ``get_annotations``).

        This replaces N parallel per-page fetches from parma's view
        rendering; the single ``in_()`` query keeps us at O(1) round trips.
        """
        result: dict[str, list[AnnotationEvent]] = {pid: [] for pid in page_ids}
        if not page_ids:
            return result
        query = (
            self.client.table("annotation_events").select("*").in_("target_page_id", list(page_ids))
        )
        if self.project_id:
            query = query.eq("project_id", self.project_id)
        query = self._staged_filter(query)
        rows = _rows(await self._execute(query))
        for r in rows:
            pid = r.get("target_page_id")
            if pid is None or pid not in result:
                continue
            result[pid].append(_row_to_annotation_event(r))
        return result

    async def save_epistemic_score(
        self,
        page_id: str,
        call_id: str,
        credence: int | None = None,
        robustness: int | None = None,
        reasoning: str = "",
        source_page_id: str | None = None,
    ) -> None:
        """Persist credence and/or robustness updates via mutation events.

        The ``epistemic_scores`` table was folded into ``mutation_events`` in
        migration ``20260418152516_fold_epistemic_scores_into_mutation_events``.
        Each score is now recorded as a ``set_credence`` or ``set_robustness``
        event, and non-staged runs also dual-write the score + reasoning
        directly onto the ``pages`` row so other readers see the update
        immediately. Supply at least one of ``credence`` / ``robustness``.
        """
        if credence is None and robustness is None:
            raise ValueError("save_epistemic_score requires at least one of credence or robustness")
        existing = await self.get_page(page_id)
        old_credence = existing.credence if existing else None
        old_robustness = existing.robustness if existing else None
        old_credence_reasoning = existing.credence_reasoning if existing else None
        old_robustness_reasoning = existing.robustness_reasoning if existing else None
        page_updates: dict[str, Any] = {}
        if credence is not None:
            payload: dict[str, Any] = {
                "value": int(credence),
                "reasoning": reasoning,
                "call_id": call_id,
                "old_value": old_credence,
                "old_reasoning": old_credence_reasoning,
            }
            if source_page_id is not None:
                payload["source_page_id"] = source_page_id
            await self.record_mutation_event("set_credence", page_id, payload)
            page_updates["credence"] = int(credence)
            page_updates["credence_reasoning"] = reasoning
        if robustness is not None:
            payload = {
                "value": int(robustness),
                "reasoning": reasoning,
                "call_id": call_id,
                "old_value": old_robustness,
                "old_reasoning": old_robustness_reasoning,
            }
            if source_page_id is not None:
                payload["source_page_id"] = source_page_id
            await self.record_mutation_event("set_robustness", page_id, payload)
            page_updates["robustness"] = int(robustness)
            page_updates["robustness_reasoning"] = reasoning
        if page_updates and not self.staged:
            await self._execute(self.client.table("pages").update(page_updates).eq("id", page_id))

    async def save_page_format_events(self, call_id: str, events: Sequence[dict[str, Any]]) -> None:
        """Batch-insert page-format tracking events."""
        if not events:
            return
        rows = [
            {
                "id": str(uuid.uuid4()),
                "page_id": e["page_id"],
                "detail": e["detail"],
                "call_id": call_id,
                "run_id": self.run_id,
                "staged": self.staged,
                "tags": e.get("tags", {}),
            }
            for e in events
        ]
        await self._execute(self.client.table("page_format_events").insert(rows))

    async def get_page_format_events_for_run(self, run_id: str) -> Sequence[dict[str, Any]]:
        """Fetch all page-format events for a run, with call_type from calls."""
        rows = _rows(
            await self._execute(
                self.client.table("page_format_events")
                .select("page_id,detail,call_id,tags")
                .eq("run_id", run_id)
            )
        )
        if not rows:
            return []
        call_ids = list({r["call_id"] for r in rows})
        call_rows = _rows(
            await self._execute(
                self.client.table("calls").select("id,call_type").in_("id", call_ids)
            )
        )
        call_type_map = {r["id"]: r["call_type"] for r in call_rows}
        for r in rows:
            r["call_type"] = call_type_map.get(r["call_id"], "unknown")
        return rows

    async def get_epistemic_score_source(
        self,
        page_id: str,
    ) -> tuple[dict[str, Any] | None, Page | None]:
        """Return the latest epistemic score entry and its source judgement (if any).

        After the ``epistemic_scores`` table was folded into ``mutation_events``,
        we reconstruct a score row from the most recent ``set_credence`` /
        ``set_robustness`` events emitted by this run. Returns
        ``(score_row, judgement_page)`` where either or both may be ``None``.
        The ``score_row`` mirrors the legacy shape ``{credence, robustness,
        call_id, reasoning, source_page_id}``.
        """
        rows = _rows(
            await self._execute(
                self.client.table("mutation_events")
                .select("event_type, payload, created_at")
                .eq("target_id", page_id)
                .eq("run_id", self.run_id)
                .in_("event_type", ["set_credence", "set_robustness"])
                .order("created_at", desc=True)
            )
        )
        if not rows:
            return None, None
        latest_credence = next((r for r in rows if r["event_type"] == "set_credence"), None)
        latest_robustness = next((r for r in rows if r["event_type"] == "set_robustness"), None)
        latest = rows[0]
        payload = latest.get("payload") or {}
        credence_payload = (latest_credence or {}).get("payload") or {}
        robustness_payload = (latest_robustness or {}).get("payload") or {}
        score_row: dict[str, Any] = {
            "credence": credence_payload.get("value"),
            "robustness": robustness_payload.get("value"),
            "call_id": payload.get("call_id"),
            "reasoning": payload.get("reasoning", ""),
            "source_page_id": payload.get("source_page_id"),
        }
        call_id = payload.get("call_id")
        if not call_id:
            return score_row, None
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
        return await self.calls.get_latest_judgement_for_call(call_id)

    async def get_root_questions(
        self,
        workspace: Workspace = Workspace.RESEARCH,
        *,
        include_duplicates: bool = False,
    ) -> list[Page]:
        """Return questions that have no parent (top-level questions).

        Triage-confirmed duplicates (``extra.triage.is_duplicate == True``)
        are hidden by default. Pass ``include_duplicates=True`` to see
        them — useful for operator/debug surfaces.
        """
        params: dict[str, Any] = {"ws": workspace.value}
        if self.project_id:
            params["pid"] = self.project_id
        if self.staged:
            params["p_staged_run_id"] = self.run_id
        if self.snapshot_ts is not None:
            params["p_snapshot_ts"] = self.snapshot_ts.isoformat()
        if include_duplicates:
            params["p_include_duplicates"] = True
        rows = _rows(await self._execute(self.client.rpc("get_root_questions", params)))
        pages = [_row_to_page(r) for r in rows]
        return await self._apply_page_events(pages)

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

    async def get_project_stats(self, project_id: str) -> dict[str, Any]:
        return await self.projects.get_project_stats(project_id)

    async def get_question_stats(self, question_id: str) -> dict[str, Any]:
        return await self.projects.get_question_stats(question_id)

    async def get_assess_staleness(
        self,
        question_ids: Sequence[str],
    ) -> dict[str, bool]:
        """Check whether questions need re-assessment.

        A question is stale if it has no completed ASSESS call, or if any
        link targeting it was created after the most recent completed ASSESS
        call's created_at.

        Returns a dict mapping each question_id to True (stale) or False.
        """
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
        calls_result = await self._execute(calls_query)

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
        links_query = self._staged_filter(links_query)
        links_result = await self._execute(links_query)
        links = [_row_to_link(r) for r in _rows(links_result)]
        links = await self._apply_link_events(links)

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
        if self.project_id:
            query = query.eq("project_id", self.project_id)
        query = self._staged_filter(query)
        result = await self._execute(query)
        return result.count or 0

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
        """Save a suggestion to the database."""
        await self._execute(
            self.client.table("suggestions").upsert(
                {
                    "id": suggestion.id,
                    "project_id": suggestion.project_id or self.project_id,
                    "workspace": suggestion.workspace,
                    "run_id": suggestion.run_id or self.run_id,
                    "suggestion_type": suggestion.suggestion_type.value,
                    "target_page_id": suggestion.target_page_id,
                    "source_page_id": suggestion.source_page_id,
                    "payload": suggestion.payload,
                    "status": suggestion.status.value,
                    "created_at": suggestion.created_at.isoformat(),
                    "reviewed_at": (
                        suggestion.reviewed_at.isoformat() if suggestion.reviewed_at else None
                    ),
                    "staged": suggestion.staged,
                }
            )
        )

    async def get_pending_suggestions(
        self,
        target_page_id: str | None = None,
    ) -> list[Suggestion]:
        """Get pending suggestions, optionally filtered by target page."""
        query = (
            self.client.table("suggestions")
            .select("*")
            .eq("project_id", self.project_id)
            .eq("status", "pending")
        )
        if target_page_id:
            query = query.eq("target_page_id", target_page_id)
        query = self._staged_filter(query)
        query = query.order("created_at", desc=True)
        rows = _rows(await self._execute(query))
        return [_row_to_suggestion(r) for r in rows]

    async def get_suggestions(
        self,
        status: str = "pending",
        target_page_id: str | None = None,
    ) -> list[Suggestion]:
        """Get suggestions filtered by status, optionally by target page."""
        query = (
            self.client.table("suggestions")
            .select("*")
            .eq("project_id", self.project_id)
            .eq("status", status)
        )
        if target_page_id:
            query = query.eq("target_page_id", target_page_id)
        query = self._staged_filter(query)
        query = query.order("created_at", desc=True)
        rows = _rows(await self._execute(query))
        return [_row_to_suggestion(r) for r in rows]

    async def get_suggestion(self, suggestion_id: str) -> Suggestion | None:
        """Fetch a single suggestion by ID."""
        rows = _rows(
            await self._execute(
                self.client.table("suggestions").select("*").eq("id", suggestion_id)
            )
        )
        return _row_to_suggestion(rows[0]) if rows else None

    async def update_suggestion_status(
        self,
        suggestion_id: str,
        status: SuggestionStatus,
    ) -> None:
        """Update a suggestion's status (accept/reject/dismiss)."""
        update: dict[str, Any] = {"status": status.value}
        if status != SuggestionStatus.PENDING:
            update["reviewed_at"] = datetime.now(UTC).isoformat()
        await self._execute(self.client.table("suggestions").update(update).eq("id", suggestion_id))

    async def create_chat_conversation(
        self,
        project_id: str,
        question_id: str | None = None,
        title: str = "",
    ) -> ChatConversation:
        """Create a new chat conversation row."""
        conv = ChatConversation(
            project_id=project_id,
            question_id=question_id,
            title=title,
            staged=self.staged,
            run_id=self.run_id if self.staged else None,
        )
        await self._execute(
            self.client.table("chat_conversations").insert(
                {
                    "id": conv.id,
                    "project_id": conv.project_id,
                    "question_id": conv.question_id,
                    "title": conv.title,
                    "created_at": conv.created_at.isoformat(),
                    "updated_at": conv.updated_at.isoformat(),
                    "staged": conv.staged,
                    "run_id": conv.run_id,
                }
            )
        )
        return conv

    async def get_chat_conversation(self, conversation_id: str) -> ChatConversation | None:
        """Fetch a single conversation (staged-run-aware, excludes soft-deleted)."""
        query = (
            self.client.table("chat_conversations")
            .select("*")
            .eq("id", conversation_id)
            .is_("deleted_at", "null")
        )
        query = self._staged_filter(query)
        rows = _rows(await self._execute(query))
        return _row_to_chat_conversation(rows[0]) if rows else None

    async def list_chat_conversations(
        self,
        project_id: str,
        limit: int = 50,
        offset: int = 0,
        question_id: str | None = None,
    ) -> Sequence[ChatConversation]:
        """List conversations for a project, most-recently-updated first."""
        query = (
            self.client.table("chat_conversations")
            .select("*")
            .eq("project_id", project_id)
            .is_("deleted_at", "null")
        )
        if question_id:
            query = query.eq("question_id", question_id)
        query = self._staged_filter(query).order("updated_at", desc=True)
        query = query.range(offset, offset + max(0, limit - 1))
        rows = _rows(await self._execute(query))
        return [_row_to_chat_conversation(r) for r in rows]

    async def update_chat_conversation(
        self,
        conversation_id: str,
        title: str | None = None,
        touch: bool = False,
    ) -> None:
        """Rename or touch updated_at on a conversation."""
        update: dict[str, Any] = {}
        if title is not None:
            update["title"] = title
        if touch or title is not None:
            update["updated_at"] = datetime.now(UTC).isoformat()
        if not update:
            return
        await self._execute(
            self.client.table("chat_conversations").update(update).eq("id", conversation_id)
        )

    async def soft_delete_chat_conversation(self, conversation_id: str) -> None:
        """Mark a conversation as soft-deleted."""
        await self._execute(
            self.client.table("chat_conversations")
            .update({"deleted_at": datetime.now(UTC).isoformat()})
            .eq("id", conversation_id)
        )

    async def save_chat_message(
        self,
        conversation_id: str,
        role: ChatMessageRole,
        content: dict,
        seq: int | None = None,
        question_id: str | None = None,
    ) -> ChatMessage:
        """Append a message to a conversation. Auto-assigns seq if omitted.

        `question_id` records which research question this turn was asked
        against. Conversations can span multiple questions within a project,
        so this is per-message rather than per-conversation.
        """
        if seq is None:
            seq = await self._next_chat_message_seq(conversation_id)
        msg = ChatMessage(
            conversation_id=conversation_id,
            role=role,
            content=content,
            seq=seq,
            staged=self.staged,
            run_id=self.run_id if self.staged else None,
            question_id=question_id,
        )
        await self._execute(
            self.client.table("chat_messages").insert(
                {
                    "id": msg.id,
                    "conversation_id": msg.conversation_id,
                    "role": msg.role.value,
                    "content": msg.content,
                    "seq": msg.seq,
                    "ts": msg.ts.isoformat(),
                    "staged": msg.staged,
                    "run_id": msg.run_id,
                    "question_id": msg.question_id,
                }
            )
        )
        return msg

    async def _next_chat_message_seq(self, conversation_id: str) -> int:
        """Return the next sequence number for a conversation."""
        rows = _rows(
            await self._execute(
                self.client.table("chat_messages")
                .select("seq")
                .eq("conversation_id", conversation_id)
                .order("seq", desc=True)
                .limit(1)
            )
        )
        return (rows[0]["seq"] + 1) if rows else 0

    async def list_chat_messages(
        self,
        conversation_id: str,
    ) -> Sequence[ChatMessage]:
        """List all messages in a conversation in order."""
        query = (
            self.client.table("chat_messages").select("*").eq("conversation_id", conversation_id)
        )
        query = self._staged_filter(query).order("seq", desc=False)
        rows = _rows(await self._execute(query))
        return [_row_to_chat_message(r) for r in rows]

    async def branch_chat_conversation(
        self,
        source_conversation_id: str,
        at_seq: int,
        title: str | None = None,
    ) -> ChatConversation:
        """Branch a conversation at `at_seq`, copying messages 0..at_seq into a new convo.

        The source conversation is left untouched — branching is non-destructive,
        the whole point is to preserve the original thread while forking a new
        one from a chosen point. Returns the newly-created conversation.

        Raises:
            ValueError: if the source conversation does not exist, or if
                `at_seq` is negative, or if no message in the source has a
                seq <= at_seq (i.e. the branch point is before any message).
        """
        source = await self.get_chat_conversation(source_conversation_id)
        if source is None:
            raise ValueError(f"source conversation {source_conversation_id} not found")
        if at_seq < 0:
            raise ValueError(f"at_seq must be >= 0, got {at_seq}")

        messages = await self.list_chat_messages(source_conversation_id)
        if not messages:
            raise ValueError(
                f"at_seq={at_seq} does not correspond to any message in "
                f"conversation {source_conversation_id}"
            )
        max_seq = messages[-1].seq
        if at_seq > max_seq:
            # Reject out-of-range branch points. Clients specify a message
            # explicitly, so "branch at seq 999" when the last seq is 7 is
            # almost certainly a bug (stale UI state, off-by-one, etc.).
            # Fail loud rather than silently cloning the whole conversation.
            raise ValueError(
                f"at_seq={at_seq} does not correspond to any message in "
                f"conversation {source_conversation_id} (max seq is {max_seq})"
            )
        to_copy = [m for m in messages if m.seq <= at_seq]
        if not to_copy:
            raise ValueError(
                f"at_seq={at_seq} does not correspond to any message in "
                f"conversation {source_conversation_id}"
            )

        effective_seq = to_copy[-1].seq
        derived_title = title or f"branch of {source.title or '(untitled)'} @ msg {effective_seq}"

        new_conv = ChatConversation(
            project_id=source.project_id,
            question_id=source.question_id,
            title=derived_title,
            staged=self.staged,
            run_id=self.run_id if self.staged else None,
            parent_conversation_id=source.id,
            branched_at_seq=effective_seq,
        )
        await self._execute(
            self.client.table("chat_conversations").insert(
                {
                    "id": new_conv.id,
                    "project_id": new_conv.project_id,
                    "question_id": new_conv.question_id,
                    "title": new_conv.title,
                    "created_at": new_conv.created_at.isoformat(),
                    "updated_at": new_conv.updated_at.isoformat(),
                    "staged": new_conv.staged,
                    "run_id": new_conv.run_id,
                    "parent_conversation_id": new_conv.parent_conversation_id,
                    "branched_at_seq": new_conv.branched_at_seq,
                }
            )
        )

        # Bulk-insert copies of the messages. We deliberately allocate fresh
        # primary keys (so the new rows are independent of the source) but
        # preserve role/content/seq/question_id — the UI relies on seq to
        # render chronological order, and on question_id for the off-question
        # tags. ts is set to now() so the new conversation's activity reflects
        # when the branch happened, not when the original was written.
        now_iso = datetime.now(UTC).isoformat()
        new_rows = [
            {
                "id": str(uuid.uuid4()),
                "conversation_id": new_conv.id,
                "role": m.role.value,
                "content": m.content,
                "seq": m.seq,
                "ts": now_iso,
                "staged": new_conv.staged,
                "run_id": new_conv.run_id,
                "question_id": m.question_id,
            }
            for m in to_copy
        ]
        if new_rows:
            await self._execute(self.client.table("chat_messages").insert(new_rows))

        return new_conv


def _row_to_chat_conversation(row: dict[str, Any]) -> ChatConversation:
    return ChatConversation(
        id=row["id"],
        project_id=row["project_id"],
        question_id=row.get("question_id"),
        title=row.get("title") or "",
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        deleted_at=row.get("deleted_at"),
        staged=row.get("staged", False),
        run_id=row.get("run_id"),
        parent_conversation_id=row.get("parent_conversation_id"),
        branched_at_seq=row.get("branched_at_seq"),
    )


def _row_to_chat_message(row: dict[str, Any]) -> ChatMessage:
    return ChatMessage(
        id=row["id"],
        conversation_id=row["conversation_id"],
        role=ChatMessageRole(row["role"]),
        content=row.get("content") or {},
        seq=row.get("seq", 0),
        ts=row["ts"],
        staged=row.get("staged", False),
        run_id=row.get("run_id"),
        question_id=row.get("question_id"),
    )
