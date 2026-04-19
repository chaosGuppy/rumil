"""Mutation log: staged-run visibility filter + event recording + replay.

``MutationLog`` owns everything that makes staged runs work as a
capability rather than a soup of methods on ``DB``:

- ``record(event_type, target_id, payload)`` writes a ``mutation_events``
  row and invalidates the in-process cache.
- ``load_state()`` returns the decoded ``MutationState`` visible to
  this run (own events + pre-snapshot baseline events, with post-snapshot
  baseline events recorded as "unapply" entries).
- ``apply_page_events(pages)`` / ``apply_link_events(links)`` overlay
  the state onto a read batch.
- ``staged_filter(query)`` applies the base-table visibility filter
  (staged=false baseline rows plus own-run rows).

The capability still lives inside ``DB`` — ``PageStore`` / ``LinkStore``
consume it via ``self._db.mutation_log`` / the legacy shim methods. A
later phase can introduce a ``ReadOnlyClient`` wrapper that refuses
``.update()`` / ``.delete()`` on the mutation-tracked tables, making
"never bypass event recording" a structural guarantee rather than a
code-review convention.
"""

import time
import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from rumil.db.row_helpers import _rows
from rumil.models import LinkRole, Page, PageLink

if TYPE_CHECKING:
    from rumil.database import DB


class MutationState:
    """Cached mutation events for a staged run, keyed by target_id.

    The "forward" fields (``superseded_pages`` etc.) replay events *visible*
    to the staged run — its own events plus baseline events up to
    ``snapshot_ts``. The "unapply" fields undo baseline mutations that were
    *written directly to the base tables* after the snapshot: the base
    rows now reflect post-snapshot state that the staged run must not see,
    and we use the mutation event log to revert them on read.
    """

    __slots__ = (
        "credence_overrides",
        "deleted_links",
        "link_role_overrides",
        "page_content_overrides",
        "robustness_overrides",
        "superseded_pages",
        "unapply_credence",
        "unapply_robustness",
        "unapply_role_overrides",
        "unapply_supersessions",
        "unapply_update_content",
    )

    def __init__(self) -> None:
        self.superseded_pages: dict[str, str] = {}
        self.deleted_links: set[str] = set()
        self.link_role_overrides: dict[str, LinkRole] = {}
        self.page_content_overrides: dict[str, str] = {}
        self.credence_overrides: dict[str, tuple[int | None, str | None]] = {}
        self.robustness_overrides: dict[str, tuple[int | None, str | None]] = {}
        self.unapply_supersessions: set[str] = set()
        self.unapply_update_content: dict[str, str] = {}
        self.unapply_credence: dict[str, tuple[int | None, str | None]] = {}
        self.unapply_robustness: dict[str, tuple[int | None, str | None]] = {}
        self.unapply_role_overrides: dict[str, LinkRole] = {}


class MutationLog:
    """Staged-runs capability on top of the ``mutation_events`` table."""

    _CACHE_TTL_S = 5.0

    def __init__(self, db: "DB") -> None:
        self._db = db
        self._cache: MutationState | None = None
        self._cache_ts: float = 0.0

    @property
    def client(self) -> Any:
        return self._db.client

    def invalidate_cache(self) -> None:
        self._cache = None
        self._cache_ts = 0.0

    def staged_filter(self, query: Any) -> Any:
        """Apply staged-run visibility filter to a query.

        Staged runs see baseline (staged=false) + their own rows. When a
        ``snapshot_ts`` is pinned, baseline rows must additionally have been
        created at or before that instant. Non-staged runs see only baseline.
        """
        if self._db.staged:
            if self._db.snapshot_ts is not None:
                ts = self._db.snapshot_ts.isoformat()
                return query.or_(
                    f"and(staged.eq.false,created_at.lte.{ts}),run_id.eq.{self._db.run_id}"
                )
            return query.or_(f"staged.eq.false,run_id.eq.{self._db.run_id}")
        return query.eq("staged", False)

    async def record(
        self,
        event_type: str,
        target_id: str,
        payload: dict,
    ) -> None:
        """Record a mutation event for undo/staging support."""
        await self._db._execute(
            self.client.table("mutation_events").insert(
                {
                    "id": str(uuid.uuid4()),
                    "run_id": self._db.run_id,
                    "event_type": event_type,
                    "target_id": target_id,
                    "payload": payload,
                }
            )
        )
        self.invalidate_cache()

    async def load_state(self) -> MutationState:
        """Fetch and cache mutation events visible to this staged run.

        Own-run events are always forwarded onto the view. When a
        ``snapshot_ts`` is pinned, events committed by other runs split
        two ways: pre-snapshot baseline events are forwarded normally;
        post-snapshot baseline events are recorded as "unapply" entries
        that ``apply_*_events`` uses to roll base-table values back to
        their pre-mutation state on read.

        Without a snapshot, only own-run events are included.
        """
        now = time.monotonic()
        if self._cache is not None and now - self._cache_ts < self._CACHE_TTL_S:
            return self._cache
        if not self._db.staged:
            self._cache = MutationState()
            self._cache_ts = now
            return self._cache
        own_rows = _rows(
            await self._db._execute(
                self.client.table("mutation_events")
                .select("event_type, target_id, payload, created_at, run_id")
                .eq("run_id", self._db.run_id)
                .order("created_at")
            )
        )
        baseline_rows: list[dict[str, Any]] = []
        post_snapshot_rows: list[dict[str, Any]] = []
        if self._db.snapshot_ts is not None:
            ts = self._db.snapshot_ts.isoformat()
            baseline_rows = _rows(
                await self._db._execute(
                    self.client.table("mutation_events")
                    .select("event_type, target_id, payload, created_at, run_id")
                    .neq("run_id", self._db.run_id)
                    .lte("created_at", ts)
                    .order("created_at")
                )
            )
            post_snapshot_rows = _rows(
                await self._db._execute(
                    self.client.table("mutation_events")
                    .select("event_type, target_id, payload, created_at, run_id")
                    .neq("run_id", self._db.run_id)
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
        post_sorted = sorted(
            post_snapshot_rows,
            key=lambda r: r.get("created_at") or "",
        )
        for row in post_sorted:
            et = row["event_type"]
            tid = row["target_id"]
            payload = row.get("payload") or {}
            if et == "supersede_page":
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
        self._cache = state
        self._cache_ts = now
        return state

    async def apply_page_events(self, pages: Sequence[Page]) -> list[Page]:
        """Overlay mutation events onto a batch of pages."""
        state = await self.load_state()
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

    async def apply_link_events(self, links: Sequence[PageLink]) -> list[PageLink]:
        """Overlay mutation events onto a batch of links."""
        state = await self.load_state()
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
