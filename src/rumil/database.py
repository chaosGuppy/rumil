"""
Supabase database layer for the research workspace.
"""

import asyncio
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

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

from rumil.models import (
    SCOUT_CALL_TYPES,
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
from rumil.settings import get_settings
from supabase import AsyncClient, acreate_client

if TYPE_CHECKING:
    from rumil.forks import ForkRow

# Supabase SDK types APIResponse.data as JSON | None, but table queries
# always return list[dict]. We cast to this alias for clarity.
log = logging.getLogger(__name__)

_Rows = list[dict[str, Any]]


def _rows(response: Any) -> _Rows:
    """Extract rows from a Supabase API response with proper typing."""
    return cast(_Rows, response.data) if response.data else []


@dataclass(frozen=True)
class PrioritizationCandidate:
    """A prioritization call that had a given question in its dispatchable
    candidate set — i.e. its scope was the question itself, or the question
    was a direct CHILD_QUESTION child of the prio call's scope at query time.
    """

    call_id: str
    run_id: str
    scope_page_id: str
    scope_headline: str
    created_at: datetime
    is_scope: bool


@dataclass(frozen=True)
class QuestionBudgetPool:
    """Per-question shared budget pool state.

    Multiple prioritisation cycles working on the same question contribute
    their assigned budget to the pool and draw from it together. The pool
    is the authoritative stop signal for prio loops; the run-level budget
    remains the authoritative ceiling.

    ``registered=False`` indicates that no pool row exists — the caller
    bypassed ``qbp_register`` (e.g. ``scripts/run_prio.py`` running
    ``get_dispatches`` outside ``run()``). Bailout checks should treat
    this as "no pool gate" rather than "drained to zero", to match
    ``qbp_consume``'s sentinel behaviour for the same case.
    """

    question_id: str
    contributed: int
    consumed: int
    active_calls: int
    registered: bool = True

    @property
    def remaining(self) -> int:
        return self.contributed - self.consumed


_DB_RETRYABLE_EXCEPTIONS = (
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.ConnectError,
    httpx.ReadError,
    httpx.RemoteProtocolError,
)


_PG_QUERY_CANCELED_SQLSTATE = "57014"
_PG_UNIQUE_VIOLATION_SQLSTATE = "23505"
_FORK_SAMPLE_INDEX_MAX_ATTEMPTS = 10


def _is_statement_timeout(exc: BaseException) -> bool:
    """Postgres SQLSTATE 57014 (query_canceled) — fires when a query exceeds
    the role's statement_timeout. Load-induced under heavy concurrency; worth
    retrying a few times, but with a tighter cap than other DB errors so we
    don't mask a genuinely-slow query."""
    return isinstance(exc, APIError) and exc.code == _PG_QUERY_CANCELED_SQLSTATE


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
    return (
        isinstance(exc, _DB_RETRYABLE_EXCEPTIONS)
        or _is_retryable_api_error(exc)
        or _is_statement_timeout(exc)
    )


def _exception_class(exc: BaseException | None) -> str:
    """Bucket retryable DB exceptions by retry-budget class."""
    return "timeout" if exc is not None and _is_statement_timeout(exc) else "other"


def _bump_class_attempt(retry_state: RetryCallState, klass: str) -> int:
    """Increment a per-class attempt counter on the retry state and return
    the new count. Tenacity creates a fresh ``RetryCallState`` per wrapped
    call, so the counters reset between calls but persist across retries
    within a single call. Used to give 57014 its own retry budget without
    consuming the larger ``max_db_retries`` cap."""
    counts = getattr(retry_state, "_db_attempts_by_class", None)
    if counts is None:
        counts = {"timeout": 0, "other": 0}
        retry_state._db_attempts_by_class = counts  # type: ignore[attr-defined]
    counts[klass] += 1
    return counts[klass]


def _cap_for_class(klass: str) -> int:
    settings = get_settings()
    if klass == "timeout":
        return settings.max_db_statement_timeout_retries
    return settings.max_db_retries


def _stop_after_db_retries(retry_state: RetryCallState) -> bool:
    # Each retryable exception class has its own attempt counter and cap, so
    # a 57014 following a string of 503s still gets its full timeout budget
    # rather than inheriting the 503s' attempt count.
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    klass = _exception_class(exc)
    n = _bump_class_attempt(retry_state, klass)
    return n >= _cap_for_class(klass)


def _log_db_retry(retry_state: RetryCallState) -> None:
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    wait = retry_state.next_action.sleep if retry_state.next_action else 0
    klass = _exception_class(exc)
    counts = getattr(retry_state, "_db_attempts_by_class", {})
    log.warning(
        "DB request failed (%s), retrying in %gs (%s attempt %d/%d)",
        type(exc).__name__ if exc else "unknown",
        wait,
        klass,
        counts.get(klass, retry_state.attempt_number),
        _cap_for_class(klass),
    )


_db_retry = retry(
    retry=retry_if_exception(_should_retry_db_exception),
    stop=_stop_after_db_retries,
    wait=wait_exponential(multiplier=0.5, min=0.5, max=60),
    before_sleep=_log_db_retry,
    reraise=True,
)


_LINK_COLUMNS = (
    "id,from_page_id,to_page_id,link_type,direction,"
    "strength,reasoning,role,importance,section,position,"
    "impact_on_parent_question,created_at,run_id,scope_question_id"
)

_SLIM_PAGE_COLUMNS = (
    "id,page_type,layer,workspace,headline,abstract,"
    "epistemic_status,epistemic_type,credence,credence_reasoning,"
    "robustness,robustness_reasoning,extra,is_superseded,"
    "project_id,created_at,superseded_by,run_id,hidden,scope_question_id"
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
        credence_reasoning=row.get("credence_reasoning"),
        robustness=row.get("robustness"),
        robustness_reasoning=row.get("robustness_reasoning"),
        provenance_model=row.get("provenance_model") or "",
        provenance_call_type=row.get("provenance_call_type") or "",
        provenance_call_id=row.get("provenance_call_id") or "",
        created_at=datetime.fromisoformat(row["created_at"]),
        superseded_by=row.get("superseded_by"),
        is_superseded=bool(row.get("is_superseded", False)),
        extra=row.get("extra") or {},
        abstract=row.get("abstract") or "",
        fruit_remaining=row.get("fruit_remaining"),
        sections=row.get("sections"),
        meta_type=row.get("meta_type"),
        run_id=row.get("run_id") or "",
        hidden=bool(row.get("hidden", False)),
        scope_question_id=row.get("scope_question_id"),
    )


def _row_to_link(row: dict[str, Any]) -> PageLink:
    return PageLink(
        id=row["id"],
        from_page_id=row["from_page_id"],
        to_page_id=row["to_page_id"],
        link_type=LinkType(row["link_type"]),
        direction=(ConsiderationDirection(row["direction"]) if row["direction"] else None),
        strength=row["strength"],
        reasoning=row["reasoning"] or "",
        role=LinkRole(row.get("role", "direct")),
        importance=row.get("importance"),
        section=row.get("section"),
        position=row.get("position"),
        impact_on_parent_question=row.get("impact_on_parent_question"),
        created_at=datetime.fromisoformat(row["created_at"]),
        run_id=row.get("run_id") or "",
        scope_question_id=row.get("scope_question_id"),
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
        completed_at=(datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None),
        sequence_id=row.get("sequence_id"),
        sequence_position=row.get("sequence_position"),
        cost_usd=row.get("cost_usd"),
    )


def _row_to_project(row: dict[str, Any]) -> Project:
    return Project(
        id=row["id"],
        name=row["name"],
        created_at=datetime.fromisoformat(row["created_at"]),
        hidden=row.get("hidden", False),
        owner_user_id=row.get("owner_user_id"),
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

    __slots__ = (
        "credence_source",
        "deleted_links",
        "hidden_overrides",
        "latest_credence",
        "latest_robustness",
        "link_role_overrides",
        "page_content_overrides",
        "robustness_source",
        "superseded_pages",
    )

    def __init__(self) -> None:
        self.superseded_pages: dict[str, str] = {}
        self.deleted_links: set[str] = set()
        self.link_role_overrides: dict[str, LinkRole] = {}
        self.page_content_overrides: dict[str, str] = {}
        self.latest_credence: dict[str, tuple[int, str]] = {}
        self.latest_robustness: dict[str, tuple[int, str]] = {}
        self.credence_source: dict[str, str] = {}
        self.robustness_source: dict[str, str] = {}
        self.hidden_overrides: dict[str, bool] = {}


# Mutation events are scoped to a run, not a DB instance — the cache must be
# shared across every DB.fork() (and view_as_staged sibling) of the same run.
# Otherwise a child call writing a mutation on its forked DB never invalidates
# the parent orchestrator's cache, and reads on the parent return stale state.
# Keying on run_id ensures invalidation propagates to every fork automatically.
_MUTATION_CACHES: dict[str, MutationState] = {}
# Non-staged runs have no events to overlay; share one empty state so we
# don't allocate a fresh one per page-read batch.
_EMPTY_MUTATION_STATE = MutationState()


def clear_mutation_caches() -> None:
    """Drop all per-run mutation caches. For test teardown."""
    _MUTATION_CACHES.clear()


class _Unset:
    """Sentinel type so callers can pass ``None`` explicitly without it being
    confused with 'argument omitted'. Used by ``DB.fork`` to distinguish
    'inherit the parent's scope' from 'clear the scope'."""


_UNSET = _Unset()


class DB:
    def __init__(
        self,
        run_id: str,
        client: AsyncClient,
        project_id: str = "",
        staged: bool = False,
        scope_question_id: str | None = None,
    ):
        self.run_id = run_id
        self.client = client
        self.project_id = project_id
        self.staged = staged
        self.scope_question_id = scope_question_id
        self._semaphore = asyncio.Semaphore(get_settings().db_max_concurrent_queries)
        self._prod: bool = False

    @classmethod
    async def create(
        cls,
        run_id: str,
        prod: bool = False,
        project_id: str = "",
        client: AsyncClient | None = None,
        staged: bool = False,
        scope_question_id: str | None = None,
    ) -> "DB":
        if client is None:
            url, key = get_settings().get_supabase_credentials(prod)
            client = await acreate_client(url, key, options=AsyncClientOptions(schema="public"))
        db = cls(
            run_id=run_id,
            client=client,
            project_id=project_id,
            staged=staged,
            scope_question_id=scope_question_id,
        )
        db._prod = prod
        return db

    async def fork(self, scope_question_id: str | None | _Unset = _UNSET) -> "DB":
        """Create a new DB instance with a fresh Supabase client.

        Shares run_id, project_id, and staged flag with the parent but gets
        its own HTTP connection. Use this to scope connections to a single
        call, avoiding HTTP/2 stream exhaustion on long-running jobs.

        Pass ``scope_question_id`` to enter a scoped view: reads will be
        filtered to rows where ``scope_question_id IS NULL`` or matches the
        passed value. Pass ``None`` explicitly to clear an inherited scope.
        Omit the argument to inherit the parent's scope unchanged.
        """
        url, key = get_settings().get_supabase_credentials(self._prod)
        client = await acreate_client(url, key, options=AsyncClientOptions(schema="public"))
        resolved_scope = (
            self.scope_question_id if isinstance(scope_question_id, _Unset) else scope_question_id
        )
        db = DB(
            run_id=self.run_id,
            client=client,
            project_id=self.project_id,
            staged=self.staged,
            scope_question_id=resolved_scope,
        )
        db._prod = self._prod
        return db

    def view_as_staged(self, run_id: str) -> "DB":
        """Return a sibling DB with staged visibility for ``run_id``.

        Reuses the same Supabase client (no new HTTP connection, no close
        needed) and only flips the staging flags. Use this for short-lived
        reads that need to see a staged run's mutations — e.g. surfacing a
        staged run's pages in the trace-tree API.
        """
        db = DB(
            run_id=run_id,
            client=self.client,
            project_id=self.project_id,
            staged=True,
            scope_question_id=self.scope_question_id,
        )
        db._prod = self._prod
        return db

    def with_scope(self, scope_question_id: str | None) -> "DB":
        """Return a sibling DB with a different page-scope visibility.

        Reuses the same Supabase client (no new HTTP connection, no close
        needed) and only swaps the scope filter. Use this when a call entry
        point already runs on a parent's HTTP client and just needs to
        narrow its read visibility — e.g. ``run_prioritization_call``
        scoping reads to its target question.
        """
        db = DB(
            run_id=self.run_id,
            client=self.client,
            project_id=self.project_id,
            staged=self.staged,
            scope_question_id=scope_question_id,
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

        Staged runs see baseline (staged=false) + their own rows.
        Non-staged runs see only baseline rows.
        """
        if self.staged:
            return query.or_(f"staged.eq.false,run_id.eq.{self.run_id}")
        return query.eq("staged", False)

    def _scope_filter(self, query: Any) -> Any:
        """Apply page-scope visibility filter to a query.

        Unscoped DBs (``scope_question_id is None``) see every row — no
        filter is applied. Scoped DBs see rows where ``scope_question_id``
        is NULL (unscoped row) or matches the DB's scope. Mirrors the SQL
        predicate used by the discovery RPCs (match_pages,
        get_root_questions).
        """
        if self.scope_question_id is None:
            return query
        return query.or_(f"scope_question_id.is.null,scope_question_id.eq.{self.scope_question_id}")

    async def _load_mutation_state(self) -> MutationState:
        """Fetch and cache mutation events for this staged run."""
        if not self.staged:
            return _EMPTY_MUTATION_STATE
        cached = _MUTATION_CACHES.get(self.run_id)
        if cached is not None:
            return cached
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
            elif et == "update_page_content":
                state.page_content_overrides[tid] = payload.get("new_content", "")
            elif et == "set_credence":
                value = payload.get("value")
                if value is not None:
                    state.latest_credence[tid] = (
                        int(value),
                        payload.get("reasoning") or "",
                    )
                    source = payload.get("source_page_id")
                    if source:
                        state.credence_source[tid] = source
            elif et == "set_robustness":
                value = payload.get("value")
                if value is not None:
                    state.latest_robustness[tid] = (
                        int(value),
                        payload.get("reasoning") or "",
                    )
                    source = payload.get("source_page_id")
                    if source:
                        state.robustness_source[tid] = source
            elif et == "set_hidden" and "hidden" in payload:
                state.hidden_overrides[tid] = bool(payload["hidden"])
        _MUTATION_CACHES[self.run_id] = state
        return state

    def _invalidate_mutation_cache(self) -> None:
        _MUTATION_CACHES.pop(self.run_id, None)

    async def _apply_page_events(self, pages: Sequence[Page]) -> list[Page]:
        """Overlay mutation events onto a batch of pages."""
        state = await self._load_mutation_state()
        if (
            not state.superseded_pages
            and not state.page_content_overrides
            and not state.latest_credence
            and not state.latest_robustness
            and not state.hidden_overrides
        ):
            return list(pages)
        result: list[Page] = []
        for p in pages:
            updates: dict = {}
            if p.id in state.superseded_pages:
                updates["is_superseded"] = True
                updates["superseded_by"] = state.superseded_pages[p.id]
            if p.id in state.page_content_overrides:
                updates["content"] = state.page_content_overrides[p.id]
            if p.id in state.latest_credence:
                value, reasoning = state.latest_credence[p.id]
                updates["credence"] = value
                updates["credence_reasoning"] = reasoning
            if p.id in state.latest_robustness:
                value, reasoning = state.latest_robustness[p.id]
                updates["robustness"] = value
                updates["robustness_reasoning"] = reasoning
            if p.id in state.hidden_overrides:
                updates["hidden"] = state.hidden_overrides[p.id]
            if updates:
                p = p.model_copy(update=updates)
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
                link = link.model_copy(
                    update={
                        "role": state.link_role_overrides[link.id],
                    }
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

    async def get_or_create_project(
        self,
        name: str,
        owner_user_id: str | None = None,
    ) -> Project:
        rows = _rows(
            await self._execute(self.client.table("projects").select("*").eq("name", name))
        )
        if rows:
            return _row_to_project(rows[0])
        insert: dict[str, str] = {"name": name}
        if owner_user_id:
            insert["owner_user_id"] = owner_user_id
        row = _rows(await self._execute(self.client.table("projects").insert(insert)))[0]
        return _row_to_project(row)

    async def list_projects(
        self,
        include_hidden: bool = False,
        owner_user_id: str | None = None,
    ) -> list[Project]:
        # PostgREST's max-rows caps .limit() at 1000, so we paginate with
        # range() until we get a short page. Without this, newly created
        # workspaces fall off the end once test-* projects accumulate past
        # 1000 — list_projects would silently return a stale prefix.
        page_size = 1000
        start = 0
        rows: list[dict] = []
        while True:
            query = self.client.table("projects").select("*").order("created_at")
            if not include_hidden:
                query = query.eq("hidden", False)
            if owner_user_id:
                query = query.eq("owner_user_id", owner_user_id)
            page = _rows(await self._execute(query.range(start, start + page_size - 1)))
            rows.extend(page)
            if len(page) < page_size:
                break
            start += page_size
        return [_row_to_project(r) for r in rows]

    async def is_admin_user(self, user_id: str) -> bool:
        if not user_id:
            return False
        rows = _rows(
            await self._execute(
                self.client.table("user_admins").select("user_id").eq("user_id", user_id).limit(1)
            )
        )
        return bool(rows)

    async def grant_admin(
        self,
        user_id: str,
        granted_by: str | None = None,
        note: str | None = None,
    ) -> None:
        payload: dict[str, str] = {"user_id": user_id}
        if granted_by:
            payload["granted_by"] = granted_by
        if note:
            payload["note"] = note
        await self._execute(self.client.table("user_admins").upsert(payload, on_conflict="user_id"))

    async def revoke_admin(self, user_id: str) -> None:
        await self._execute(self.client.table("user_admins").delete().eq("user_id", user_id))

    async def list_admin_users(self) -> list[dict[str, Any]]:
        """user_admins rows enriched with email from the Supabase Auth admin API.

        PostgREST does not expose the `auth` schema, so we fetch emails
        through `client.auth.admin.get_user_by_id` rather than a direct join.
        Returns list of {user_id, email, granted_at, granted_by, note}.
        """
        rows = _rows(
            await self._execute(
                self.client.table("user_admins")
                .select("user_id, granted_at, granted_by, note")
                .order("granted_at", desc=True)
            )
        )
        out: list[dict[str, Any]] = []
        for r in rows:
            email = ""
            try:
                resp = await self.client.auth.admin.get_user_by_id(r["user_id"])
                email = (resp.user.email if resp and resp.user else "") or ""
            except Exception:
                pass
            out.append({**r, "email": email})
        return out

    async def find_auth_user_id_by_email(self, email: str) -> str | None:
        """Linear scan via the Auth admin API (no email-filter endpoint)."""
        target = email.strip().lower()
        page = 1
        per_page = 200
        while True:
            users = await self.client.auth.admin.list_users(page=page, per_page=per_page) or []
            if not users:
                return None
            for u in users:
                u_email = (getattr(u, "email", "") or "").strip().lower()
                if u_email == target:
                    return getattr(u, "id", None)
            if len(users) < per_page:
                return None
            page += 1

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
                    "fruit_remaining": page.fruit_remaining,
                    "sections": page.sections,
                    "meta_type": page.meta_type,
                    "run_id": self.run_id,
                    "staged": self.staged,
                    "abstract": page.abstract,
                    "hidden": page.hidden,
                    "scope_question_id": page.scope_question_id,
                }
            )
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

    async def update_epistemic_score(
        self,
        page_id: str,
        *,
        credence: int | None = None,
        credence_reasoning: str | None = None,
        robustness: int | None = None,
        robustness_reasoning: str | None = None,
        source_page_id: str | None = None,
    ) -> None:
        """Record a credence and/or robustness update.

        Each non-null score becomes one ``set_credence`` or ``set_robustness``
        mutation event. For non-staged runs, the baseline ``pages`` columns
        are dual-written so non-replay readers see the new value. Reasoning
        is required whenever the paired score is provided.
        """
        if credence is None and robustness is None:
            return
        direct_updates: dict[str, Any] = {}
        if credence is not None:
            if credence_reasoning is None:
                raise ValueError(
                    "update_epistemic_score: credence_reasoning is required when credence is set"
                )
            payload: dict[str, Any] = {
                "value": int(credence),
                "reasoning": credence_reasoning,
            }
            if source_page_id is not None:
                payload["source_page_id"] = source_page_id
            await self.record_mutation_event("set_credence", page_id, payload)
            direct_updates["credence"] = int(credence)
            direct_updates["credence_reasoning"] = credence_reasoning
        if robustness is not None:
            if robustness_reasoning is None:
                raise ValueError(
                    "update_epistemic_score: robustness_reasoning is required "
                    "when robustness is set"
                )
            payload = {
                "value": int(robustness),
                "reasoning": robustness_reasoning,
            }
            if source_page_id is not None:
                payload["source_page_id"] = source_page_id
            await self.record_mutation_event("set_robustness", page_id, payload)
            direct_updates["robustness"] = int(robustness)
            direct_updates["robustness_reasoning"] = robustness_reasoning
        if direct_updates and not self.staged:
            await self._execute(self.client.table("pages").update(direct_updates).eq("id", page_id))

    async def update_page_abstract(self, page_id: str, abstract: str) -> None:
        await self._execute(
            self.client.table("pages").update({"abstract": abstract}).eq("id", page_id)
        )

    async def get_page(self, page_id: str) -> Page | None:
        query = self.client.table("pages").select("*").eq("id", page_id)
        query = self._scope_filter(self._staged_filter(query))
        rows = _rows(await self._execute(query))
        if not rows:
            return None
        pages = await self._apply_page_events([_row_to_page(rows[0])])
        if not pages:
            return None
        return pages[0]

    async def get_page_staging_info(self, page_id: str) -> tuple[bool, str, str] | None:
        """Return (staged, run_id, project_id) for a page, bypassing the staged
        visibility filter. Returns None if the page doesn't exist.

        Callers use this to discover whether a page is staged under another run
        before deciding which run_id/staged combination to open a DB with.
        """
        rows = _rows(
            await self._execute(
                self.client.table("pages").select("staged, run_id, project_id").eq("id", page_id)
            )
        )
        if not rows:
            return None
        row = rows[0]
        return (
            bool(row.get("staged")),
            row.get("run_id") or "",
            row.get("project_id") or "",
        )

    async def get_pages_by_ids(self, page_ids: Sequence[str]) -> dict[str, Page]:
        """Bulk-fetch pages by ID. Returns {id: Page} for pages that exist."""
        if not page_ids:
            return {}
        result: dict[str, Page] = {}
        id_list = list(page_ids)
        batch_size = 200
        for start in range(0, len(id_list), batch_size):
            batch = id_list[start : start + batch_size]
            rows = _rows(
                await self._execute(
                    self._scope_filter(
                        self._staged_filter(self.client.table("pages").select("*").in_("id", batch))
                    )
                )
            )
            for r in rows:
                page = _row_to_page(r)
                result[page.id] = page
        pages = await self._apply_page_events(list(result.values()))
        return {p.id: p for p in pages}

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
        8-char short IDs. Returns the full UUID if found, or None.

        For longer strings that miss exact match, falls back to a prefix
        match on the first 8 characters — this catches UUID-mistyping
        (e.g. an LLM transposing a middle segment) so long as the first
        8 hex chars are correct and unambiguous.
        """
        if not page_id:
            log.debug("resolve_page_id: empty page_id")
            return None
        # Try exact match first
        rows = _rows(await self._execute(self.client.table("pages").select("id").eq("id", page_id)))
        if rows:
            log.debug("resolve_page_id: exact match for %s", page_id[:8])
            return rows[0]["id"]
        # Try prefix match for short IDs, or fall back to first-8-char
        # prefix for longer inputs that just missed exact match.
        if len(page_id) <= 8 or not page_id.startswith("http"):
            prefix = page_id if len(page_id) <= 8 else page_id[:8]
            rows = _rows(
                await self._execute(
                    self.client.table("pages").select("id").like("id", f"{prefix}%")
                )
            )
            if len(rows) == 1:
                resolved = rows[0]["id"]
                if len(page_id) > 8 and resolved != page_id:
                    log.info(
                        "resolve_page_id: %s missed exact, recovered via first-8-char prefix to %s",
                        page_id,
                        resolved[:8],
                    )
                else:
                    log.debug(
                        "resolve_page_id: prefix match %s -> %s",
                        prefix,
                        resolved[:8],
                    )
                return resolved
            if len(rows) > 1:
                log.warning(
                    "Ambiguous short ID '%s' matches %d pages",
                    prefix,
                    len(rows),
                )
                return None
            if len(page_id) <= 8:
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
        """Resolve a call ID to a full UUID. Handles both full UUIDs and
        8-char short IDs. Returns the full UUID if found, or None."""
        if not call_id:
            return None
        rows = _rows(await self._execute(self.client.table("calls").select("id").eq("id", call_id)))
        if rows:
            return rows[0]["id"]
        if len(call_id) <= 8:
            rows = _rows(
                await self._execute(
                    self.client.table("calls").select("id").like("id", f"{call_id}%")
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

    async def get_pages_slim(
        self,
        active_only: bool = True,
        include_hidden: bool = False,
    ) -> list[Page]:
        """Fetch all pages without the content field — safe for bulk loads."""
        query = self.client.table("pages").select(_SLIM_PAGE_COLUMNS)
        if self.project_id:
            query = query.eq("project_id", self.project_id)
        if active_only:
            query = query.eq("is_superseded", False)
        if not include_hidden:
            query = query.eq("hidden", False)
        query = self._scope_filter(self._staged_filter(query))
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
        include_hidden: bool = False,
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
        if not include_hidden:
            query = query.eq("hidden", False)
        query = self._scope_filter(self._staged_filter(query))
        pages = [
            _row_to_page(r)
            for r in _rows(await self._execute(query.order("created_at", desc=True).limit(10000)))
        ]
        pages = await self._apply_page_events(pages)
        if active_only:
            pages = [p for p in pages if p.is_active()]
        return pages

    async def set_page_hidden(self, page_id: str, hidden: bool) -> None:
        """Flip a page's hidden flag, recording a mutation event.

        Staged runs only record the event (other readers keep seeing the
        baseline flag). Non-staged runs additionally update the row.
        """
        await self.record_mutation_event(
            "set_hidden",
            page_id,
            {"hidden": bool(hidden)},
        )
        if not self.staged:
            await self._execute(
                self.client.table("pages").update({"hidden": bool(hidden)}).eq("id", page_id)
            )

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
        include_hidden: bool = False,
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
        if not include_hidden:
            query = query.eq("hidden", False)
        if search:
            query = query.or_(f"headline.ilike.%{search}%,content.ilike.%{search}%")
        query = self._scope_filter(self._staged_filter(query))
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
        # First-write-wins dedup: if an active link with the same identity
        # quadruple (from, to, link_type, direction) is already visible to
        # this DB's run/staged scope, skip the insert. Re-saving by the same
        # link.id falls through to the upsert path so explicit updates still
        # work. The DB has no uniqueness index yet, so this is the only
        # guard against accidental duplicates from concurrent or repeated
        # save_link calls.
        existing = await self._find_active_link(
            from_page_id=link.from_page_id,
            to_page_id=link.to_page_id,
            link_type=link.link_type,
            direction=link.direction,
        )
        if existing is not None and existing.id != link.id:
            log.debug(
                "save_link: dedup — %s -> %s (%s) already exists as %s",
                link.from_page_id[:8],
                link.to_page_id[:8],
                link.link_type.value,
                existing.id[:8],
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
                    "scope_question_id": link.scope_question_id,
                }
            )
        )

    async def _find_active_link(
        self,
        *,
        from_page_id: str,
        to_page_id: str,
        link_type: LinkType,
        direction: ConsiderationDirection | None,
    ) -> PageLink | None:
        """Return any link visible in this DB's scope matching the identity
        quadruple, or None. Honors staged-runs visibility (baseline + own
        staged rows for staged DBs; baseline only for non-staged) via
        ``get_links_from``'s built-in filter and event overlay.
        """
        direction_value = direction.value if direction else None
        for link in await self.get_links_from(from_page_id):
            if link.to_page_id != to_page_id:
                continue
            if link.link_type != link_type:
                continue
            existing_dir = link.direction.value if link.direction else None
            if existing_dir != direction_value:
                continue
            return link
        return None

    async def get_link(self, link_id: str) -> PageLink | None:
        query = self.client.table("page_links").select("*").eq("id", link_id)
        query = self._scope_filter(self._staged_filter(query))
        rows = _rows(await self._execute(query))
        if not rows:
            return None
        links = await self._apply_link_events([_row_to_link(rows[0])])
        return links[0] if links else None

    async def get_links_to(self, page_id: str) -> list[PageLink]:
        query = self.client.table("page_links").select("*").eq("to_page_id", page_id)
        query = self._scope_filter(self._staged_filter(query))
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
        query = self._scope_filter(self._staged_filter(query))
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
        query = self._scope_filter(self._staged_filter(query))
        rows = _rows(await self._execute(query))
        return await self._apply_link_events([_row_to_link(r) for r in rows])

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
                query = self._scope_filter(self._staged_filter(query))
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
                query = self._scope_filter(self._staged_filter(query))
                rows = _rows(await self._execute(query.range(offset, offset + page_size - 1)))
                all_links.extend(_row_to_link(r) for r in rows)
                if len(rows) < page_size:
                    break
                offset += page_size
        applied = await self._apply_link_events(all_links)
        for link in applied:
            result.setdefault(link.to_page_id, []).append(link)
        return result

    async def get_latest_summary_for_question(self, question_id: str) -> "Page | None":
        """Return the most recent active SUMMARY page linked to a question."""
        links = await self.get_links_to(question_id)
        summary_links = [l for l in links if l.link_type == LinkType.SUMMARIZES]
        if not summary_links:
            return None
        pages = await self.get_pages_by_ids([l.from_page_id for l in summary_links])
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

    async def get_latest_summaries_for_questions(
        self,
        question_ids: Sequence[str],
    ) -> dict[str, Page | None]:
        """Bulk-fetch the most recent active SUMMARY page for many questions.

        Returns {question_id: summary_page_or_None}. Issues two batched queries
        (links + pages) regardless of input size.
        """
        result: dict[str, Page | None] = {qid: None for qid in question_ids}
        if not question_ids:
            return result
        id_list = list(dict.fromkeys(question_ids))
        links_by_target = await self.get_links_to_many(id_list)
        summary_from_ids: list[str] = []
        summary_links_by_question: dict[str, list[PageLink]] = {}
        for qid in id_list:
            qlinks = [l for l in links_by_target.get(qid, []) if l.link_type == LinkType.SUMMARIZES]
            if qlinks:
                summary_links_by_question[qid] = qlinks
                summary_from_ids.extend(l.from_page_id for l in qlinks)
        if not summary_from_ids:
            return result
        pages = await self.get_pages_by_ids(list(dict.fromkeys(summary_from_ids)))
        for qid, qlinks in summary_links_by_question.items():
            candidates = [
                pages[l.from_page_id]
                for l in qlinks
                if l.from_page_id in pages
                and pages[l.from_page_id].is_active()
                and pages[l.from_page_id].page_type == PageType.SUMMARY
            ]
            if candidates:
                result[qid] = max(candidates, key=lambda p: p.created_at)
        return result

    async def get_considerations_for_question(
        self,
        question_id: str,
        include_hidden: bool = False,
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
            if l.from_page_id in pages
            and pages[l.from_page_id].is_active()
            and (include_hidden or not pages[l.from_page_id].hidden)
        ]

    async def get_considerations_for_questions(
        self,
        question_ids: Sequence[str],
        include_hidden: bool = False,
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
            if page and page.is_active() and (include_hidden or not page.hidden):
                result[link.to_page_id].append((page, link))
        return result

    async def get_parent_question(
        self,
        question_id: str,
        include_hidden: bool = False,
    ) -> Page | None:
        """Return the parent question, or None if this is a root question."""
        links = await self.get_links_to(question_id)
        for link in links:
            if link.link_type == LinkType.CHILD_QUESTION:
                page = await self.get_page(link.from_page_id)
                if page and page.is_active() and (include_hidden or not page.hidden):
                    return page
        return None

    async def get_child_questions(
        self,
        parent_id: str,
        include_hidden: bool = False,
    ) -> list[Page]:
        """Return sub-questions of a question."""
        links = await self.get_links_from(parent_id)
        child_links = [l for l in links if l.link_type == LinkType.CHILD_QUESTION]
        if not child_links:
            return []
        pages = await self.get_pages_by_ids([l.to_page_id for l in child_links])
        return [
            pages[l.to_page_id]
            for l in child_links
            if l.to_page_id in pages
            and pages[l.to_page_id].is_active()
            and (include_hidden or not pages[l.to_page_id].hidden)
        ]

    async def get_child_questions_with_links(
        self,
        parent_id: str,
        include_hidden: bool = False,
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
            if l.to_page_id in pages
            and pages[l.to_page_id].is_active()
            and (include_hidden or not pages[l.to_page_id].hidden)
        ]

    async def get_judgements_for_question(
        self,
        question_id: str,
        include_hidden: bool = False,
    ) -> list[Page]:
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
            and (include_hidden or not pages[l.from_page_id].hidden)
        ]

    async def get_judgements_for_questions(
        self,
        question_ids: Sequence[str],
        include_hidden: bool = False,
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
                query = self._scope_filter(self._staged_filter(query))
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
            if (
                page is not None
                and page.is_active()
                and page.page_type == PageType.JUDGEMENT
                and (include_hidden or not page.hidden)
            ):
                result.setdefault(link.to_page_id, []).append(page)
        return result

    async def get_dependents(
        self,
        page_id: str,
        include_hidden: bool = False,
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
            if l.from_page_id in pages
            and pages[l.from_page_id].is_active()
            and (include_hidden or not pages[l.from_page_id].hidden)
        ]

    async def get_dependencies(
        self,
        page_id: str,
        include_hidden: bool = False,
    ) -> list[tuple[Page, PageLink]]:
        """Return (dependency_page, link) for all pages this one depends on."""
        links = await self.get_links_from(page_id)
        dep_links = [l for l in links if l.link_type == LinkType.DEPENDS_ON]
        if not dep_links:
            return []
        pages = await self.get_pages_by_ids([l.to_page_id for l in dep_links])
        return [
            (pages[l.to_page_id], l)
            for l in dep_links
            if l.to_page_id in pages and (include_hidden or not pages[l.to_page_id].hidden)
        ]

    async def _get_project_page_ids(self) -> set[str] | None:
        """Fetch all page IDs belonging to the current project.

        Returns None if no project_id is set (meaning no project scoping).
        """
        if not self.project_id:
            return None
        page_ids: set[str] = set()
        offset = 0
        page_size = 1000
        while True:
            query = self.client.table("pages").select("id").eq("project_id", self.project_id)
            query = self._scope_filter(self._staged_filter(query))
            rows = _rows(await self._execute(query.range(offset, offset + page_size - 1)))
            page_ids.update(r["id"] for r in rows)
            if len(rows) < page_size:
                break
            offset += page_size
        return page_ids

    async def _get_depends_on_links_for_pages(
        self,
        page_ids: set[str] | None,
    ) -> list[PageLink]:
        """Fetch DEPENDS_ON links, optionally scoped to pages in page_ids.

        When page_ids is provided, fetches links whose from_page_id is in
        the set using batched in_() queries. When None, fetches all
        DEPENDS_ON links with pagination.
        """
        all_links: list[PageLink] = []
        if page_ids is not None:
            id_list = list(page_ids)
            batch_size = 100
            page_size = 1000
            for start in range(0, len(id_list), batch_size):
                batch = id_list[start : start + batch_size]
                offset = 0
                while True:
                    query = (
                        self.client.table("page_links")
                        .select("*")
                        .eq("link_type", "depends_on")
                        .in_("from_page_id", batch)
                    )
                    query = self._scope_filter(self._staged_filter(query))
                    rows = _rows(await self._execute(query.range(offset, offset + page_size - 1)))
                    all_links.extend(_row_to_link(r) for r in rows)
                    if len(rows) < page_size:
                        break
                    offset += page_size
        else:
            offset = 0
            page_size = 1000
            while True:
                query = self.client.table("page_links").select("*").eq("link_type", "depends_on")
                query = self._scope_filter(self._staged_filter(query))
                rows = _rows(await self._execute(query.range(offset, offset + page_size - 1)))
                all_links.extend(_row_to_link(r) for r in rows)
                if len(rows) < page_size:
                    break
                offset += page_size
        return await self._apply_link_events(all_links)

    async def get_stale_dependencies(self) -> list[tuple[PageLink, int | None]]:
        """Return DEPENDS_ON links where the dependency has been superseded.

        Returns (link, change_magnitude) pairs. change_magnitude comes from
        the supersession mutation event if available, otherwise None.

        Scoped to the current project when project_id is set. Issues
        O(ceil(N_project_pages/batch_size)) round trips for the link query,
        plus batched lookups for target pages and supersession magnitudes.
        """
        project_page_ids = await self._get_project_page_ids()
        links = await self._get_depends_on_links_for_pages(project_page_ids)
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

        Scoped to the current project when project_id is set.
        `page_links` has no `project_id` column, so we resolve project membership
        via `pages`.
        """
        project_page_ids = await self._get_project_page_ids()
        links = await self._get_depends_on_links_for_pages(project_page_ids)

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
        call_params: dict | None = None,
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
            context_page_ids=list(context_page_ids) if context_page_ids else [],
            sequence_id=sequence_id,
            sequence_position=sequence_position,
            call_params=call_params,
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
                    "completed_at": (call.completed_at.isoformat() if call.completed_at else None),
                    "run_id": self.run_id,
                    "sequence_id": call.sequence_id,
                    "sequence_position": call.sequence_position,
                    "cost_usd": call.cost_usd,
                }
            )
        )

    async def get_call(self, call_id: str) -> Call | None:
        rows = _rows(await self._execute(self.client.table("calls").select("*").eq("id", call_id)))
        return _row_to_call(rows[0]) if rows else None

    async def update_call_status(
        self,
        call_id: str,
        status: CallStatus,
        result_summary: str = "",
        call_params: dict | None = None,
        cost_usd: float | None = None,
    ) -> None:
        completed_at = datetime.now(UTC).isoformat() if status == CallStatus.COMPLETE else None
        payload: dict = {
            "status": status.value,
            "result_summary": result_summary,
            "completed_at": completed_at,
        }
        if call_params is not None:
            payload["call_params"] = call_params
        if cost_usd is not None:
            payload["cost_usd"] = cost_usd
        await self._execute(self.client.table("calls").update(payload).eq("id", call_id))

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
                self.client.table("budget").select("total, used").eq("run_id", self.run_id)
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

    async def qbp_get(self, question_id: str) -> QuestionBudgetPool:
        """Read the current pool state for a question.

        Returns a zero-default object when no pool row exists.
        """
        rows = _rows(
            await self._execute(
                self.client.table("question_budget_pool")
                .select("contributed, consumed, active_calls")
                .eq("run_id", self.run_id)
                .eq("question_id", question_id)
            )
        )
        if not rows:
            return QuestionBudgetPool(
                question_id=question_id,
                contributed=0,
                consumed=0,
                active_calls=0,
                registered=False,
            )
        r = rows[0]
        return QuestionBudgetPool(
            question_id=question_id,
            contributed=r["contributed"],
            consumed=r["consumed"],
            active_calls=r["active_calls"],
        )

    async def qbp_get_many(self, question_ids: Sequence[str]) -> dict[str, QuestionBudgetPool]:
        """Batched pool fetch. Missing IDs are absent from the returned dict."""
        if not question_ids:
            return {}
        rows = _rows(
            await self._execute(
                self.client.table("question_budget_pool")
                .select("question_id, contributed, consumed, active_calls")
                .eq("run_id", self.run_id)
                .in_("question_id", list(question_ids))
            )
        )
        return {
            r["question_id"]: QuestionBudgetPool(
                question_id=r["question_id"],
                contributed=r["contributed"],
                consumed=r["consumed"],
                active_calls=r["active_calls"],
            )
            for r in rows
        }

    async def qbp_register(self, question_id: str, contribution: int) -> QuestionBudgetPool:
        """Add a contribution and increment active_calls for the pool."""
        result = await self._execute(
            self.client.rpc(
                "qbp_register",
                {
                    "rid": self.run_id,
                    "qid": question_id,
                    "contribution": contribution,
                },
            )
        )
        rows = cast(list[dict[str, Any]], result.data) or []
        if not rows:
            return await self.qbp_get(question_id)
        r = rows[0]
        return QuestionBudgetPool(
            question_id=question_id,
            contributed=r["contributed"],
            consumed=r["consumed"],
            active_calls=r["active_calls"],
        )

    async def qbp_consume(self, question_id: str, amount: int = 1) -> tuple[int, bool]:
        """Atomically debit the pool. Returns (remaining, exhausted).

        When no pool row exists, returns a sentinel large positive remaining
        and ``exhausted=False`` — the run-level budget is the authoritative
        gate; pool consumption never refuses.
        """
        result = await self._execute(
            self.client.rpc(
                "qbp_consume",
                {"rid": self.run_id, "qid": question_id, "amount": amount},
            )
        )
        rows = cast(list[dict[str, Any]], result.data) or []
        if not rows:
            return 2147483647, False
        r = rows[0]
        return int(r["remaining"]), bool(r["exhausted"])

    async def qbp_unregister(self, question_id: str) -> None:
        """Decrement active_calls (floored at 0). Leaves contributed/consumed."""
        await self._execute(
            self.client.rpc(
                "qbp_unregister",
                {"rid": self.run_id, "qid": question_id},
            )
        )

    async def qbp_recurse(
        self,
        parent_question_id: str,
        child_question_id: str,
        amount: int,
    ) -> None:
        """Charge the parent pool by ``amount`` and register the child contribution.

        Atomic: peer cycles never see momentarily-doubled budget.
        """
        await self._execute(
            self.client.rpc(
                "qbp_recurse",
                {
                    "rid": self.run_id,
                    "parent_qid": parent_question_id,
                    "child_qid": child_question_id,
                    "amount": amount,
                },
            )
        )

    async def qbp_refund(
        self,
        parent_question_id: str,
        child_question_id: str,
        amount: int,
    ) -> None:
        """Inverse of ``qbp_recurse``: return ``amount`` from a failed child
        cycle's allocation back to the parent pool. No-op when amount<=0.
        """
        if amount <= 0:
            return
        await self._execute(
            self.client.rpc(
                "qbp_refund",
                {
                    "rid": self.run_id,
                    "parent_qid": parent_question_id,
                    "child_qid": child_question_id,
                    "amount": amount,
                },
            )
        )

    async def get_active_calls_for_question(
        self,
        question_id: str,
        *,
        exclude_call_id: str | None = None,
    ) -> list[Call]:
        """Return pending/running calls of any type targeting a question.

        Scoped to the current ``run_id``. The optional ``exclude_call_id``
        filters out the caller's own call so a prio context can omit itself.
        """
        query = (
            self.client.table("calls")
            .select("*")
            .eq("scope_page_id", question_id)
            .eq("run_id", self.run_id)
            .in_("status", [CallStatus.PENDING.value, CallStatus.RUNNING.value])
            .order("created_at")
        )
        rows = _rows(await self._execute(query))
        calls = [_row_to_call(r) for r in rows]
        if exclude_call_id is not None:
            calls = [c for c in calls if c.id != exclude_call_id]
        return calls

    async def get_active_prio_pools_for_subquestions(
        self, parent_question_id: str
    ) -> list[tuple[str, QuestionBudgetPool]]:
        """For each direct CHILD_QUESTION of ``parent_question_id``, return
        (child_id, pool) pairs where the child has at least one active prio cycle.
        """
        children = await self.get_child_questions(parent_question_id)
        if not children:
            return []
        pools = await self.qbp_get_many([c.id for c in children])
        return [
            (c.id, pools[c.id]) for c in children if c.id in pools and pools[c.id].active_calls > 0
        ]

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
        query = self._scope_filter(self._staged_filter(query))
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
            page_ids_query = self._scope_filter(
                self._staged_filter(
                    self.client.table("pages").select("id").eq("project_id", self.project_id)
                )
            )
            page_ids_rows = _rows(await self._execute(page_ids_query.limit(50000)))
            proj_page_ids = {r["id"] for r in page_ids_rows}
            all_rows: list[dict[str, Any]] = []
            offset = 0
            while True:
                query = self.client.table("page_links").select(_LINK_COLUMNS)
                query = self._scope_filter(self._staged_filter(query))
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
                query = self._scope_filter(self._staged_filter(query))
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
                    query = self._scope_filter(self._staged_filter(query))
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
                self._scope_filter(
                    self._staged_filter(
                        self.client.table("page_links").select("*").eq("id", link_id)
                    )
                )
            )
        )
        if not rows:
            # Link is invisible to this DB's staged/scope view. Recording an
            # empty mutation event would corrupt the log (retroactive staging
            # cannot restore from an empty payload), and the DELETE below is
            # unfiltered — proceeding would silently bypass the visibility
            # filter the snapshot fetch just enforced.
            return
        link_snapshot = rows[0]
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
                .in_("call_type", [ct.value for ct in SCOUT_CALL_TYPES])
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

    async def save_call_trace(self, call_id: str, events: Sequence[dict]) -> None:
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
            await self._execute(self.client.table("calls").select("trace_json").eq("id", call_id))
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
        self,
        parent_call_id: str,
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
        self,
        sequence_id: str,
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

    async def get_prioritization_calls_with_question_as_candidate(
        self, question_id: str
    ) -> list[PrioritizationCandidate]:
        """Find prioritization calls that had ``question_id`` in their dispatchable
        candidate set.

        A prioritization call's candidates are its scope question plus the scope
        question's direct CHILD_QUESTION children (this is what ``short_id_map``
        gets seeded with in ``build_prioritization_context``). So for question Q,
        we match prio calls whose ``scope_page_id`` is Q or any current parent
        of Q via CHILD_QUESTION links. If Q has been re-parented since a prio
        call ran, that historical association is not surfaced — accepted in v1.
        """
        parent_links = [
            link
            for link in await self.get_links_to(question_id)
            if link.link_type == LinkType.CHILD_QUESTION
        ]
        candidate_scopes = list({question_id, *(l.from_page_id for l in parent_links)})

        rows = _rows(
            await self._execute(
                self.client.table("calls")
                .select("id, run_id, scope_page_id, created_at")
                .eq("call_type", CallType.PRIORITIZATION.value)
                .in_("scope_page_id", candidate_scopes)
                .order("created_at", desc=True)
            )
        )
        if not rows:
            return []

        scope_ids = {r["scope_page_id"] for r in rows if r.get("scope_page_id")}
        pages = await self.get_pages_by_ids(list(scope_ids))

        return [
            PrioritizationCandidate(
                call_id=r["id"],
                run_id=r.get("run_id") or "",
                scope_page_id=r["scope_page_id"],
                scope_headline=(
                    pages[r["scope_page_id"]].headline if r["scope_page_id"] in pages else ""
                ),
                created_at=datetime.fromisoformat(r["created_at"]),
                is_scope=r["scope_page_id"] == question_id,
            )
            for r in rows
            if r.get("scope_page_id")
        ]

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
                    "created_at": datetime.now(UTC).isoformat(),
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
                    "created_at": datetime.now(UTC).isoformat(),
                    "run_id": self.run_id,
                }
            )
        )

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

    async def latest_artefact_for_task(self, task_id: str) -> Page | None:
        """Return the most recently-created active ARTEFACT linked ARTEFACT_OF to *task_id*.

        Ties broken by page ``id`` for stable ordering. Returns None if no
        artefact exists for the task.
        """
        links = await self.get_links_to(task_id)
        artefact_links = [l for l in links if l.link_type == LinkType.ARTEFACT_OF]
        if not artefact_links:
            return None
        pages_by_id = await self.get_pages_by_ids([l.from_page_id for l in artefact_links])
        active = [
            p for p in pages_by_id.values() if p.is_active() and p.page_type == PageType.ARTEFACT
        ]
        if not active:
            return None
        return max(active, key=lambda p: (p.created_at, p.id))

    async def get_root_questions(
        self,
        workspace: Workspace = Workspace.RESEARCH,
        include_hidden: bool = False,
    ) -> list[Page]:
        """Return questions that have no parent (top-level questions)."""
        params: dict[str, Any] = {"ws": workspace.value}
        if self.project_id:
            params["pid"] = self.project_id
        if self.staged:
            params["p_staged_run_id"] = self.run_id
        if include_hidden:
            params["p_include_hidden"] = True
        if self.scope_question_id is not None:
            params["p_scope_question_id"] = self.scope_question_id
        rows = _rows(await self._execute(self.client.rpc("get_root_questions", params)))
        pages = [_row_to_page(r) for r in rows]
        pages = await self._apply_page_events(pages)
        return [p for p in pages if p.is_active()]

    async def get_human_questions(
        self,
        workspace: Workspace = Workspace.RESEARCH,
        include_hidden: bool = False,
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
        if not include_hidden:
            query = query.eq("hidden", False)
        query = self._scope_filter(self._staged_filter(query))
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
        """Compute aggregate stats for a project via the compute_project_stats RPC.

        When this DB is a staged run, the RPC is passed the run_id so baseline
        rows plus this run's staged rows are counted; mutation events for the
        run (supersede_page, delete_link) are also overlayed.
        """
        params: dict[str, Any] = {"p_project_id": project_id}
        if self.staged:
            params["p_staged_run_id"] = self.run_id
        result = await self._execute(self.client.rpc("compute_project_stats", params))
        return cast(dict[str, Any], result.data or {})

    async def get_question_stats(self, question_id: str) -> dict[str, Any]:
        """Compute aggregate stats for the 2-hop undirected neighborhood of a question.

        Returns the same JSONB shape as get_project_stats plus a subgraph_page_count
        field. Staged-run visibility mirrors get_project_stats.
        """
        params: dict[str, Any] = {"p_question_id": question_id}
        if self.staged:
            params["p_staged_run_id"] = self.run_id
        result = await self._execute(self.client.rpc("compute_question_stats", params))
        return cast(dict[str, Any], result.data or {})

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
        links_query = self._scope_filter(self._staged_filter(links_query))
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
        query = self._scope_filter(self._staged_filter(query))
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
        model: str | None = None,
        request_kwargs: dict[str, Any] | None = None,
        thinking_blocks: dict[str, Any] | None = None,
        available_tools: Sequence[dict[str, Any]] | None = None,
        response_schema: dict[str, Any] | None = None,
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
            "model": model,
        }
        if user_messages is not None:
            row["user_messages"] = user_messages
        if request_kwargs is not None:
            row["request_kwargs"] = request_kwargs
        if thinking_blocks is not None:
            row["thinking_blocks"] = thinking_blocks
        if available_tools is not None:
            row["available_tools"] = list(available_tools)
        if response_schema is not None:
            row["response_schema"] = response_schema
        await self._execute(self.client.table("call_llm_exchanges").insert(row))
        return exchange_id

    async def get_llm_exchanges(self, call_id: str) -> list[dict[str, Any]]:
        rows = _rows(
            await self._execute(
                self.client.table("call_llm_exchanges")
                .select(
                    "id, call_id, phase, round, input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens, duration_ms, error, created_at"
                )
                .eq("call_id", call_id)
                .order("round")
            )
        )
        return rows

    async def get_llm_exchange_totals_for_run(self, run_id: str) -> dict[str, int]:
        """Sum token counts across every llm exchange in the run.

        Returns a dict with ``input_tokens``, ``output_tokens``,
        ``cache_read_tokens``, ``cache_create_tokens`` — zero when the run has
        no exchanges or no rows match. Used by ``/api/runs/{id}/trace-tree``
        to render a run-level token/cache rollup alongside total cost.
        """
        rows = _rows(
            await self._execute(
                self.client.table("call_llm_exchanges")
                .select(
                    "input_tokens, output_tokens, "
                    "cache_creation_input_tokens, cache_read_input_tokens"
                )
                .eq("run_id", run_id)
            )
        )
        return {
            "input_tokens": sum(int(r.get("input_tokens") or 0) for r in rows),
            "output_tokens": sum(int(r.get("output_tokens") or 0) for r in rows),
            "cache_read_tokens": sum(int(r.get("cache_read_input_tokens") or 0) for r in rows),
            "cache_create_tokens": sum(
                int(r.get("cache_creation_input_tokens") or 0) for r in rows
            ),
        }

    async def get_llm_exchange(self, exchange_id: str) -> dict[str, Any] | None:
        rows = _rows(
            await self._execute(
                self.client.table("call_llm_exchanges").select("*").eq("id", exchange_id)
            )
        )
        return rows[0] if rows else None

    async def save_fork(
        self,
        *,
        base_exchange_id: str,
        overrides: dict,
        overrides_hash: str,
        model: str,
        temperature: float | None,
        response_text: str | None,
        tool_calls: Sequence[dict],
        stop_reason: str | None,
        input_tokens: int | None,
        output_tokens: int | None,
        cache_creation_input_tokens: int | None,
        cache_read_input_tokens: int | None,
        duration_ms: int | None,
        cost_usd: float | None,
        error: str | None,
        created_by: str | None,
    ) -> "ForkRow":
        from rumil.forks import ForkRow

        base_row: dict[str, Any] = {
            "base_exchange_id": base_exchange_id,
            "overrides": overrides,
            "overrides_hash": overrides_hash,
            "model": model,
            "temperature": temperature,
            "response_text": response_text,
            "tool_calls": list(tool_calls),
            "stop_reason": stop_reason,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_input_tokens": cache_creation_input_tokens,
            "cache_read_input_tokens": cache_read_input_tokens,
            "duration_ms": duration_ms,
            "cost_usd": cost_usd,
            "error": error,
            "created_by": created_by,
        }
        # Allocate sample_index inside the function with retry on UNIQUE
        # violation, so concurrent fire_fork callers can't race to the same
        # (base_exchange_id, overrides_hash, sample_index) tuple.
        for _ in range(_FORK_SAMPLE_INDEX_MAX_ATTEMPTS):
            max_idx = await self.get_max_fork_sample_index(base_exchange_id, overrides_hash)
            next_idx = (max_idx + 1) if max_idx is not None else 0
            row = {**base_row, "id": str(uuid.uuid4()), "sample_index": next_idx}
            try:
                result = await self._execute(self.client.table("exchange_forks").insert(row))
            except APIError as exc:
                if exc.code == _PG_UNIQUE_VIOLATION_SQLSTATE:
                    continue
                raise
            inserted = _rows(result)[0] if _rows(result) else row
            return ForkRow(
                id=inserted["id"],
                base_exchange_id=inserted["base_exchange_id"],
                overrides=inserted["overrides"],
                overrides_hash=inserted["overrides_hash"],
                sample_index=inserted["sample_index"],
                model=inserted["model"],
                temperature=inserted.get("temperature"),
                response_text=inserted.get("response_text"),
                tool_calls=inserted.get("tool_calls") or [],
                stop_reason=inserted.get("stop_reason"),
                input_tokens=inserted.get("input_tokens"),
                output_tokens=inserted.get("output_tokens"),
                cache_creation_input_tokens=inserted.get("cache_creation_input_tokens"),
                cache_read_input_tokens=inserted.get("cache_read_input_tokens"),
                duration_ms=inserted.get("duration_ms"),
                cost_usd=inserted.get("cost_usd"),
                error=inserted.get("error"),
                created_at=inserted.get("created_at"),
                created_by=inserted.get("created_by"),
            )
        raise RuntimeError(
            f"save_fork: could not allocate sample_index for "
            f"({base_exchange_id}, {overrides_hash}) after "
            f"{_FORK_SAMPLE_INDEX_MAX_ATTEMPTS} attempts"
        )

    async def get_max_fork_sample_index(
        self, base_exchange_id: str, overrides_hash: str
    ) -> int | None:
        rows = _rows(
            await self._execute(
                self.client.table("exchange_forks")
                .select("sample_index")
                .eq("base_exchange_id", base_exchange_id)
                .eq("overrides_hash", overrides_hash)
                .order("sample_index", desc=True)
                .limit(1)
            )
        )
        return rows[0]["sample_index"] if rows else None

    async def list_forks_for_exchange(self, base_exchange_id: str) -> list[dict[str, Any]]:
        return _rows(
            await self._execute(
                self.client.table("exchange_forks")
                .select("*")
                .eq("base_exchange_id", base_exchange_id)
                .order("created_at")
            )
        )

    async def get_fork(self, fork_id: str) -> dict[str, Any] | None:
        rows = _rows(
            await self._execute(self.client.table("exchange_forks").select("*").eq("id", fork_id))
        )
        return rows[0] if rows else None

    async def delete_fork(self, fork_id: str) -> None:
        await self._execute(self.client.table("exchange_forks").delete().eq("id", fork_id))

    async def get_call_rows_for_run(self, run_id: str) -> list[dict]:
        return _rows(
            await self._execute(
                self.client.table("calls").select("*").eq("run_id", run_id).order("created_at")
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

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        """Fetch a row from the runs table by run_id."""
        rows = _rows(await self._execute(self.client.table("runs").select("*").eq("id", run_id)))
        return rows[0] if rows else None

    async def create_run(
        self,
        name: str,
        question_id: str | None,
        config: dict | None = None,
        entrypoint: str | None = None,
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
                    "entrypoint": entrypoint,
                }
            )
        )

    async def count_run_questions(self) -> int:
        """Count question pages created by this run."""
        query = (
            self.client.table("pages")
            .select("id", count=CountMethod.exact)
            .eq("run_id", self.run_id)
            .eq("page_type", PageType.QUESTION.value)
        )
        if self.project_id:
            query = query.eq("project_id", self.project_id)
        query = self._scope_filter(self._staged_filter(query))
        result = await self._execute(query)
        return result.count or 0

    async def get_run_questions_since(
        self,
        since: datetime,
    ) -> list[Page]:
        """Return question pages created by this run after *since*."""
        query = (
            self.client.table("pages")
            .select(_SLIM_PAGE_COLUMNS)
            .eq("run_id", self.run_id)
            .eq("page_type", PageType.QUESTION.value)
            .gt("created_at", since.isoformat())
            .order("created_at")
        )
        if self.project_id:
            query = query.eq("project_id", self.project_id)
        query = self._scope_filter(self._staged_filter(query))
        result = await self._execute(query)
        pages = [_row_to_page(r) for r in _rows(result)]
        return await self._apply_page_events(pages)

    async def stage_run(self, run_id: str) -> None:
        """Retroactively stage a completed non-staged run.

        Flips the staged flag on the run's rows, then reverts direct
        mutations (supersessions, link deletions, role changes) so baseline
        readers see the pre-run state. The mutation events remain, so a
        staged reader replaying them will see the same view the run
        originally produced.
        """
        await self._execute(self.client.table("runs").update({"staged": True}).eq("id", run_id))
        await self._execute(
            self.client.table("pages").update({"staged": True}).eq("run_id", run_id)
        )
        await self._execute(
            self.client.table("page_links").update({"staged": True}).eq("run_id", run_id)
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
                    "scope_question_id": payload.get("scope_question_id"),
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
        await self._execute(
            self.client.table("pages").update({"staged": False}).eq("run_id", run_id)
        )
        await self._execute(
            self.client.table("page_links").update({"staged": False}).eq("run_id", run_id)
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
        """Save an AB evaluation report. Returns the report ID."""
        report_id = str(uuid.uuid4())
        await self._execute(
            self.client.table("ab_eval_reports").insert(
                {
                    "id": report_id,
                    "run_id_a": run_id_a,
                    "run_id_b": run_id_b,
                    "question_id_a": question_id_a,
                    "question_id_b": question_id_b,
                    "overall_assessment": overall_assessment,
                    "overall_assessment_call_id": overall_assessment_call_id,
                    "eval_run_id": self.run_id,
                    "dimension_reports": list(dimension_reports),
                    "project_id": str(self.project_id) if self.project_id else None,
                }
            )
        )
        return report_id

    async def list_ab_eval_reports(
        self,
        owner_user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List AB evaluation reports, newest first.

        When `owner_user_id` is provided, the caller-controlled filter is
        applied via the `projects.owner_user_id` FK so cross-user reports
        never leak regardless of the request's scoping project.
        """
        q = (
            self.client.table("ab_eval_reports")
            .select(
                "id, run_id_a, run_id_b, question_id_a, question_id_b, "
                "overall_assessment, dimension_reports, created_at, project_id"
            )
            .order("created_at", desc=True)
        )
        if self.project_id:
            q = q.eq("project_id", str(self.project_id))
        rows = _rows(await self._execute(q))
        if owner_user_id:
            project_ids = {r.get("project_id") for r in rows if r.get("project_id")}
            if not project_ids:
                return []
            owned = _rows(
                await self._execute(
                    self.client.table("projects")
                    .select("id")
                    .eq("owner_user_id", owner_user_id)
                    .in_("id", list(project_ids))
                )
            )
            owned_ids = {r["id"] for r in owned}
            rows = [r for r in rows if r.get("project_id") in owned_ids]
        return rows

    async def list_run_call_experiments(
        self,
        owner_user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List runs created by `scripts/run_call.py`, newest first.

        Filtered by `entrypoint = 'run_call'`. Like `list_ab_eval_reports`,
        applies `self.project_id` when set and supports an
        `owner_user_id` cross-user safety filter via
        `projects.owner_user_id`.
        """
        q = (
            self.client.table("runs")
            .select("id, name, question_id, config, staged, created_at, project_id, entrypoint")
            .eq("entrypoint", "run_call")
            .order("created_at", desc=True)
        )
        if self.project_id:
            q = q.eq("project_id", str(self.project_id))
        rows = _rows(await self._execute(q))
        if owner_user_id:
            project_ids = {r.get("project_id") for r in rows if r.get("project_id")}
            if not project_ids:
                return []
            owned = _rows(
                await self._execute(
                    self.client.table("projects")
                    .select("id")
                    .eq("owner_user_id", owner_user_id)
                    .in_("id", list(project_ids))
                )
            )
            owned_ids = {r["id"] for r in owned}
            rows = [r for r in rows if r.get("project_id") in owned_ids]
        return rows

    async def list_run_prio_experiments(
        self,
        owner_user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List runs created by `scripts/run_prio.py`, newest first.

        Filtered by `entrypoint = 'run_prio'`. Mirrors
        `list_run_call_experiments`: applies `self.project_id` when set
        and supports an `owner_user_id` cross-user safety filter via
        `projects.owner_user_id`.
        """
        q = (
            self.client.table("runs")
            .select("id, name, question_id, config, staged, created_at, project_id, entrypoint")
            .eq("entrypoint", "run_prio")
            .order("created_at", desc=True)
        )
        if self.project_id:
            q = q.eq("project_id", str(self.project_id))
        rows = _rows(await self._execute(q))
        if owner_user_id:
            project_ids = {r.get("project_id") for r in rows if r.get("project_id")}
            if not project_ids:
                return []
            owned = _rows(
                await self._execute(
                    self.client.table("projects")
                    .select("id")
                    .eq("owner_user_id", owner_user_id)
                    .in_("id", list(project_ids))
                )
            )
            owned_ids = {r["id"] for r in owned}
            rows = [r for r in rows if r.get("project_id") in owned_ids]
        return rows

    async def list_context_eval_experiments(
        self,
        owner_user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List context-builder eval gold runs that are paired with a candidate.

        Each context-eval invocation produces two runs (one gold, one
        candidate) tagged with `entrypoint = 'context_eval'`. Historical
        runs that predate the tag are backfilled by a migration. We
        surface only gold rows that have been paired
        (config.eval.paired_run_id is set), giving one entry per
        comparison.
        """
        q = (
            self.client.table("runs")
            .select("id, name, question_id, config, staged, created_at, project_id, entrypoint")
            .eq("entrypoint", "context_eval")
            .eq("config->eval->>role", "gold")
            .order("created_at", desc=True)
        )
        if self.project_id:
            q = q.eq("project_id", str(self.project_id))
        rows = _rows(await self._execute(q))
        rows = [r for r in rows if ((r.get("config") or {}).get("eval") or {}).get("paired_run_id")]
        if owner_user_id:
            project_ids = {r.get("project_id") for r in rows if r.get("project_id")}
            if not project_ids:
                return []
            owned = _rows(
                await self._execute(
                    self.client.table("projects")
                    .select("id")
                    .eq("owner_user_id", owner_user_id)
                    .in_("id", list(project_ids))
                )
            )
            owned_ids = {r["id"] for r in owned}
            rows = [r for r in rows if r.get("project_id") in owned_ids]
        return rows

    async def get_ab_eval_report(self, report_id: str) -> dict[str, Any] | None:
        """Get a single AB evaluation report by ID."""
        q = self.client.table("ab_eval_reports").select("*").eq("id", report_id)
        if self.project_id:
            q = q.eq("project_id", str(self.project_id))
        rows = _rows(await self._execute(q))
        return rows[0] if rows else None

    async def save_run_eval_report(
        self,
        run_id: str,
        question_id: str,
        overall_assessment: str,
        dimension_reports: Sequence[dict[str, Any]],
    ) -> str:
        """Save a single-run evaluation report. Returns the report ID."""
        report_id = str(uuid.uuid4())
        await self._execute(
            self.client.table("run_eval_reports").insert(
                {
                    "id": report_id,
                    "run_id": run_id,
                    "question_id": question_id,
                    "overall_assessment": overall_assessment,
                    "dimension_reports": list(dimension_reports),
                    "project_id": str(self.project_id) if self.project_id else None,
                }
            )
        )
        return report_id

    async def list_run_eval_reports(self) -> list[dict[str, Any]]:
        """List all single-run evaluation reports for this project, newest first."""
        q = (
            self.client.table("run_eval_reports")
            .select("id, run_id, question_id, overall_assessment, dimension_reports, created_at")
            .order("created_at", desc=True)
        )
        if self.project_id:
            q = q.eq("project_id", str(self.project_id))
        return _rows(await self._execute(q))

    async def get_run_eval_report(self, report_id: str) -> dict[str, Any] | None:
        """Get a single run evaluation report by ID."""
        q = self.client.table("run_eval_reports").select("*").eq("id", report_id)
        if self.project_id:
            q = q.eq("project_id", str(self.project_id))
        rows = _rows(await self._execute(q))
        return rows[0] if rows else None

    async def list_runs_for_project(self, project_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """Return recent runs for a project, newest first.

        Queries the runs table and falls back to the calls table for legacy
        runs that predate the runs table.
        """
        run_rows = _rows(
            await self._execute(
                self.client.table("runs")
                .select("id, name, question_id, config, created_at, staged")
                .eq("project_id", project_id)
                .order("created_at", desc=True)
                .limit(limit * 2)
            )
        )
        legacy_rows = _rows(
            await self._execute(
                self.client.table("calls")
                .select("run_id, created_at, scope_page_id")
                .eq("project_id", project_id)
                .is_("parent_call_id", "null")
                .order("created_at", desc=True)
            )
        )

        page_ids: set[str] = set()
        for row in run_rows:
            qid = row.get("question_id")
            if qid:
                page_ids.add(qid)
        for row in legacy_rows:
            scope_id = row.get("scope_page_id")
            if scope_id:
                page_ids.add(scope_id)
        pages_by_id = await self.get_pages_by_ids(list(page_ids)) if page_ids else {}

        results: list[dict[str, Any]] = []
        seen_run_ids: set[str] = set()
        for row in run_rows:
            qid = row.get("question_id")
            page = pages_by_id.get(qid) if qid else None
            results.append(
                {
                    "run_id": row["id"],
                    "created_at": row["created_at"],
                    "name": row.get("name", ""),
                    "config": row.get("config", {}),
                    "question_summary": page.headline if page else None,
                    "staged": row.get("staged", False),
                }
            )
            seen_run_ids.add(row["id"])

        seen_legacy: set[str] = set()
        for row in legacy_rows:
            rid = row.get("run_id")
            if not rid or rid in seen_run_ids or rid in seen_legacy:
                continue
            seen_legacy.add(rid)
            scope_id = row.get("scope_page_id")
            page = pages_by_id.get(scope_id) if scope_id else None
            results.append(
                {
                    "run_id": rid,
                    "created_at": row["created_at"],
                    "question_summary": page.headline if page else None,
                }
            )
        results.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        return results[:limit]

    async def list_recent_runs(
        self,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """Return recent runs across all projects, newest first, with total count.

        Skips the legacy-calls fallback used by list_runs_for_project — pre-runs-table
        research is only visible in the per-project view.
        """
        end = offset + limit - 1
        result = await self._execute(
            self.client.table("runs")
            .select(
                "id, name, question_id, project_id, config, created_at, staged",
                count=CountMethod.exact,
            )
            .order("created_at", desc=True)
            .range(offset, end)
        )
        run_rows = _rows(result)
        total = result.count or 0

        page_ids = {r["question_id"] for r in run_rows if r.get("question_id")}
        project_ids = {r["project_id"] for r in run_rows if r.get("project_id")}
        pages_by_id = await self.get_pages_by_ids(list(page_ids)) if page_ids else {}

        projects_by_id: dict[str, str] = {}
        if project_ids:
            proj_rows = _rows(
                await self._execute(
                    self.client.table("projects").select("id, name").in_("id", list(project_ids))
                )
            )
            projects_by_id = {p["id"]: p["name"] for p in proj_rows}

        results: list[dict[str, Any]] = []
        for row in run_rows:
            qid = row.get("question_id")
            page = pages_by_id.get(qid) if qid else None
            pid = row.get("project_id")
            results.append(
                {
                    "run_id": row["id"],
                    "created_at": row["created_at"],
                    "name": row.get("name", ""),
                    "config": row.get("config", {}),
                    "question_summary": page.headline if page else None,
                    "staged": row.get("staged", False),
                    "project_id": pid,
                    "project_name": projects_by_id.get(pid) if pid else None,
                }
            )
        return results, total

    async def find_eval_gold_run(
        self,
        question_id: str,
        builder_name: str = "ImpactFilteredContext",
    ) -> str | None:
        """Find the most recent context-eval gold run for this question.

        Looks at runs.config.eval (a tag written by the context-builder
        evaluation workflow) and returns the run_id of the newest gold-role
        run for the given builder, scoped to this DB's project. Returns
        None if no matching run exists.
        """
        if not self.project_id:
            return None
        rows = _rows(
            await self._execute(
                self.client.table("runs")
                .select("id")
                .eq("project_id", str(self.project_id))
                .eq("question_id", question_id)
                .eq("config->eval->>role", "gold")
                .eq("config->eval->>context_builder", builder_name)
                .order("created_at", desc=True)
                .limit(1)
            )
        )
        return rows[0]["id"] if rows else None

    async def set_run_eval_meta(
        self,
        run_id: str,
        *,
        role: str,
        context_builder: str,
        question_id: str,
        paired_run_id: str | None = None,
    ) -> None:
        """Tag a runs row with the context-builder-eval metadata block.

        Called by the eval workflow only AFTER the call completes
        successfully — so a run whose build_context phase failed never
        gets the eval.role tag, and find_eval_gold_run won't surface it
        as a usable cache hit.
        """
        rows = _rows(
            await self._execute(self.client.table("runs").select("config").eq("id", run_id))
        )
        if not rows:
            return
        config = dict(rows[0].get("config") or {})
        config["eval"] = {
            "role": role,
            "context_builder": context_builder,
            "paired_run_id": paired_run_id,
            "question_id": question_id,
        }
        await self._execute(self.client.table("runs").update({"config": config}).eq("id", run_id))

    async def update_run_config_eval_partner(
        self,
        run_id: str,
        partner_run_id: str,
    ) -> None:
        """Patch an existing run's config.eval.paired_run_id field.

        Used by the context-builder eval workflow once both arms exist, so
        the gold row also points at its candidate partner.
        """
        rows = _rows(
            await self._execute(self.client.table("runs").select("config").eq("id", run_id))
        )
        if not rows:
            return
        config = dict(rows[0].get("config") or {})
        eval_meta = dict(config.get("eval") or {})
        eval_meta["paired_run_id"] = partner_run_id
        config["eval"] = eval_meta
        await self._execute(self.client.table("runs").update({"config": config}).eq("id", run_id))

    async def delete_run_data(self, delete_project: bool = False) -> None:
        """Delete all data for this run_id. Used by test teardown."""
        await self._execute(self.client.table("mutation_events").delete().eq("run_id", self.run_id))
        await self._execute(
            self.client.table("call_llm_exchanges").delete().eq("run_id", self.run_id)
        )
        for table in ["page_flags", "page_ratings", "page_links"]:
            await self._execute(self.client.table(table).delete().eq("run_id", self.run_id))
        # Null out sequence_id FK before deleting sequences and calls
        await self._execute(
            self.client.table("calls").update({"sequence_id": None}).eq("run_id", self.run_id)
        )
        await self._execute(self.client.table("call_sequences").delete().eq("run_id", self.run_id))
        for table in ["calls", "pages"]:
            await self._execute(self.client.table(table).delete().eq("run_id", self.run_id))
        await self._execute(self.client.table("budget").delete().eq("run_id", self.run_id))
        await self._execute(
            self.client.table("question_budget_pool").delete().eq("run_id", self.run_id)
        )
        await self._execute(self.client.table("runs").delete().eq("id", self.run_id))
        if delete_project and self.project_id:
            await self._execute(self.client.table("projects").delete().eq("id", self.project_id))
