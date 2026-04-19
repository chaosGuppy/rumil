"""ProjectStore: operations on the ``projects`` table plus stats RPCs.

The ``projects`` table does not participate in the staged-runs mutation-log
contract — project rows are always visible workspace-wide and are never
superseded, so there is no mutation-event recording here.

Stats RPCs (``compute_project_stats`` / ``compute_question_stats``) are
included in this store because they are project-level aggregates. The RPCs
*do* honor staged-run visibility in SQL via their ``p_staged_run_id`` /
``p_snapshot_ts`` parameters, so we pass the DB handle through to read
``run_id`` / ``staged`` / ``snapshot_ts``.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from rumil.db.row_helpers import _rows
from rumil.models import Project

if TYPE_CHECKING:
    from rumil.database import DB


class ProjectStore:
    """Reads and writes the ``projects`` table."""

    def __init__(self, db: "DB") -> None:
        self._db = db

    @property
    def client(self) -> Any:
        return self._db.client

    async def get_or_create_project(self, name: str) -> tuple[Project, bool]:
        """Return ``(project, created)`` for *name*.

        ``created`` is ``True`` when a new row was inserted on this call,
        ``False`` when an existing row was returned. The second flag lets the
        HTTP layer distinguish "you just made a new workspace" from "you
        reused an existing one".
        """
        rows = _rows(
            await self._db._execute(self.client.table("projects").select("*").eq("name", name))
        )
        if rows:
            row = rows[0]
            return (
                Project(
                    id=row["id"],
                    name=row["name"],
                    created_at=datetime.fromisoformat(row["created_at"]),
                    hidden=row.get("hidden", False),
                ),
                False,
            )
        row = _rows(await self._db._execute(self.client.table("projects").insert({"name": name})))[
            0
        ]
        return (
            Project(
                id=row["id"],
                name=row["name"],
                created_at=datetime.fromisoformat(row["created_at"]),
                hidden=row.get("hidden", False),
            ),
            True,
        )

    async def list_projects_summary(
        self,
        include_hidden: bool = False,
    ) -> list[dict[str, Any]]:
        """Per-project summary rows for the public landing page.

        Calls the list_projects_summary RPC (see migration
        20260418052703_projects_summary_rpc.sql). Returns raw rows with
        id/name/created_at/hidden plus question_count/claim_count/call_count
        and last_activity_at aggregated in one SQL pass.
        """
        result = await self._db._execute(
            self.client.rpc(
                "list_projects_summary",
                {"include_hidden": include_hidden},
            )
        )
        return cast(list[dict[str, Any]], result.data or [])

    async def list_projects(self, include_hidden: bool = False) -> list[Project]:
        query = self.client.table("projects").select("*").order("created_at")
        if not include_hidden:
            query = query.eq("hidden", False)
        rows = _rows(await self._db._execute(query))
        return [
            Project(
                id=r["id"],
                name=r["name"],
                created_at=datetime.fromisoformat(r["created_at"]),
                hidden=r.get("hidden", False),
            )
            for r in rows
        ]

    async def get_project(self, project_id: str) -> Project | None:
        """Return a single project row by id, or None if absent."""
        rows = _rows(
            await self._db._execute(self.client.table("projects").select("*").eq("id", project_id))
        )
        if not rows:
            return None
        r = rows[0]
        return Project(
            id=r["id"],
            name=r["name"],
            created_at=datetime.fromisoformat(r["created_at"]),
            hidden=r.get("hidden", False),
        )

    async def update_project(
        self,
        project_id: str,
        *,
        name: str | None = None,
        hidden: bool | None = None,
    ) -> Project | None:
        """Patch a project's name and/or hidden flag. Returns the refreshed row.

        Caller is responsible for trimming ``name`` and checking for collisions;
        this helper only issues the UPDATE. Returns ``None`` if the project
        doesn't exist (callers should surface 404).
        """
        update: dict[str, Any] = {}
        if name is not None:
            update["name"] = name
        if hidden is not None:
            update["hidden"] = hidden
        if not update:
            return await self.get_project(project_id)
        await self._db._execute(self.client.table("projects").update(update).eq("id", project_id))
        return await self.get_project(project_id)

    async def bulk_hide_projects(self, project_ids: Sequence[str]) -> int:
        """Soft-hide many projects in one round trip. Returns count updated.

        Mirrors ``update_project(hidden=True)`` but issues a single UPDATE
        with ``in_(...)`` instead of one query per project. No mutation
        event is recorded — the projects table doesn't participate in the
        staged-runs model.
        """
        if not project_ids:
            return 0
        await self._db._execute(
            self.client.table("projects").update({"hidden": True}).in_("id", list(project_ids))
        )
        return len(project_ids)

    async def get_project_stats(self, project_id: str) -> dict[str, Any]:
        """Compute aggregate stats for a project via the compute_project_stats RPC.

        Returns a JSONB blob (see supabase/migrations/20260411204240_add_stats_rpcs.sql
        for the shape). Staged runs see baseline plus their own rows; non-staged
        runs see baseline only.
        """
        params: dict[str, Any] = {
            "p_project_id": project_id,
            "p_staged_run_id": self._db.run_id if self._db.staged else None,
        }
        if self._db.snapshot_ts is not None:
            params["p_snapshot_ts"] = self._db.snapshot_ts.isoformat()
        result = await self._db._execute(self.client.rpc("compute_project_stats", params))
        return cast(dict[str, Any], result.data or {})

    async def get_question_stats(self, question_id: str) -> dict[str, Any]:
        """Compute aggregate stats for the 2-hop undirected neighborhood of a question.

        Returns the same JSONB shape as get_project_stats plus a subgraph_page_count
        field. Staged runs see baseline plus their own rows; non-staged runs see
        baseline only.
        """
        params: dict[str, Any] = {
            "p_question_id": question_id,
            "p_staged_run_id": self._db.run_id if self._db.staged else None,
        }
        if self._db.snapshot_ts is not None:
            params["p_snapshot_ts"] = self._db.snapshot_ts.isoformat()
        result = await self._db._execute(self.client.rpc("compute_question_stats", params))
        return cast(dict[str, Any], result.data or {})
