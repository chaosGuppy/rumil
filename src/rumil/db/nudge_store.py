"""NudgeStore: mid-run human steering rows on the ``run_nudges`` table.

Append-only from the store's perspective — callers never edit nudge rows
in place. Status transitions (active → consumed / expired / revoked)
happen via ``revoke_nudge`` and ``mark_consumed``, which just stamp the
corresponding timestamp columns.

Staging follows the same pattern as ``annotation_events`` and
``reputation_events``: each row carries ``staged`` + ``run_id`` so staged
runs see baseline plus their own rows. Nudges don't participate in the
mutation log.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from rumil.db.row_helpers import _rows
from rumil.models import (
    NudgeAuthorKind,
    NudgeDurability,
    NudgeKind,
    NudgeScope,
    NudgeStatus,
    RunNudge,
)

if TYPE_CHECKING:
    from rumil.database import DB


def _row_to_nudge(row: dict[str, Any]) -> RunNudge:
    scope_data = row.get("scope") or {}
    return RunNudge(
        id=row["id"],
        run_id=row["run_id"],
        author_kind=NudgeAuthorKind(row["author_kind"]),
        author_note=row.get("author_note") or "",
        kind=NudgeKind(row["kind"]),
        payload=row.get("payload") or {},
        durability=NudgeDurability(row["durability"]),
        scope=NudgeScope.model_validate(scope_data),
        soft_text=row.get("soft_text"),
        hard=bool(row.get("hard", False)),
        status=NudgeStatus(row["status"]),
        staged=bool(row.get("staged", False)),
        created_at=datetime.fromisoformat(row["created_at"]),
        revoked_at=(datetime.fromisoformat(row["revoked_at"]) if row.get("revoked_at") else None),
        consumed_at=(
            datetime.fromisoformat(row["consumed_at"]) if row.get("consumed_at") else None
        ),
    )


class NudgeStore:
    """CRUD + scope-matched reads on ``run_nudges``."""

    def __init__(self, db: "DB") -> None:
        self._db = db

    @property
    def client(self) -> Any:
        return self._db.client

    async def create_nudge(
        self,
        *,
        run_id: str,
        kind: NudgeKind,
        durability: NudgeDurability,
        author_kind: NudgeAuthorKind,
        author_note: str = "",
        payload: dict | None = None,
        scope: NudgeScope | None = None,
        soft_text: str | None = None,
        hard: bool = False,
    ) -> RunNudge:
        scope_json = (scope or NudgeScope()).model_dump(mode="json", exclude_none=True)
        row: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "run_id": run_id,
            "author_kind": author_kind.value,
            "author_note": author_note,
            "kind": kind.value,
            "payload": payload or {},
            "durability": durability.value,
            "scope": scope_json,
            "soft_text": soft_text,
            "hard": hard,
            "status": NudgeStatus.ACTIVE.value,
            "staged": self._db.staged,
            "created_at": datetime.now(UTC).isoformat(),
        }
        rows = _rows(await self._db._execute(self.client.table("run_nudges").insert(row)))
        return _row_to_nudge(rows[0])

    async def get_nudge(self, nudge_id: str) -> RunNudge | None:
        rows = _rows(
            await self._db._execute(self.client.table("run_nudges").select("*").eq("id", nudge_id))
        )
        return _row_to_nudge(rows[0]) if rows else None

    async def list_nudges_for_run(
        self,
        run_id: str,
        *,
        status: NudgeStatus | None = None,
    ) -> list[RunNudge]:
        q = self.client.table("run_nudges").select("*").eq("run_id", run_id)
        q = self._staged_filter(q)
        if status is not None:
            q = q.eq("status", status.value)
        q = q.order("created_at")
        rows = _rows(await self._db._execute(q))
        return [_row_to_nudge(r) for r in rows]

    async def get_active_for_run(
        self,
        run_id: str,
        *,
        call_type: str | None = None,
        question_ids: Sequence[str] | None = None,
        call_id: str | None = None,
        now: datetime | None = None,
    ) -> list[RunNudge]:
        """Return active nudges whose scope matches the given context.

        Scope match rules:
          * empty scope → always matches (project-wide for the run).
          * call_types: nudge matches if call_type arg is in the list; if
            arg is None and scope restricts, the nudge does NOT match.
          * question_ids: nudge matches if any of its question_ids appears
            in question_ids arg (intersection non-empty).
          * call_id: nudge matches only if call_id arg equals scope.call_id.
          * expires_at: filtered out if now >= expires_at.
          * expires_after_n_calls: informational only here; caller decrements
            via ``mark_consumed``/``decrement_call_budget``.

        Returns newest-first, matching the composition rule
        "hard filters union, soft concat newest-first".
        """
        all_active = await self.list_nudges_for_run(run_id, status=NudgeStatus.ACTIVE)
        now = now or datetime.now(UTC)
        matched: list[RunNudge] = []
        for nudge in all_active:
            s = nudge.scope
            if s.expires_at is not None and now >= s.expires_at:
                continue
            if s.call_types:
                if call_type is None or call_type not in s.call_types:
                    continue
            if s.question_ids:
                if not question_ids or not set(s.question_ids) & set(question_ids):
                    continue
            if s.call_id is not None:
                if call_id is None or call_id != s.call_id:
                    continue
            matched.append(nudge)
        matched.sort(key=lambda n: n.created_at, reverse=True)
        return matched

    async def revoke_nudge(self, nudge_id: str) -> RunNudge | None:
        await self._db._execute(
            self.client.table("run_nudges")
            .update(
                {
                    "status": NudgeStatus.REVOKED.value,
                    "revoked_at": datetime.now(UTC).isoformat(),
                }
            )
            .eq("id", nudge_id)
        )
        return await self.get_nudge(nudge_id)

    async def mark_consumed(self, nudge_id: str) -> RunNudge | None:
        await self._db._execute(
            self.client.table("run_nudges")
            .update(
                {
                    "status": NudgeStatus.CONSUMED.value,
                    "consumed_at": datetime.now(UTC).isoformat(),
                }
            )
            .eq("id", nudge_id)
        )
        return await self.get_nudge(nudge_id)

    async def mark_expired(self, nudge_id: str) -> RunNudge | None:
        await self._db._execute(
            self.client.table("run_nudges")
            .update({"status": NudgeStatus.EXPIRED.value})
            .eq("id", nudge_id)
        )
        return await self.get_nudge(nudge_id)

    def _staged_filter(self, query: Any) -> Any:
        """Staged runs see baseline + their own rows; non-staged see baseline only."""
        if self._db.staged:
            return query.or_(f"staged.eq.false,run_id.eq.{self._db.run_id}")
        return query.eq("staged", False)
