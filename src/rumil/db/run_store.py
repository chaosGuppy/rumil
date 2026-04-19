"""RunStore: run lifecycle + budget + eval-report persistence.

Runs are the unit of a research session — every call, page, link, and
budget consumption is scoped to a ``run_id``. Runs themselves do not
participate in the staged-runs mutation-log contract (runs own events,
they don't participate in them), so nothing here records mutation
events.

What lives here:

- Run lifecycle reads/writes (``create_run``, ``get_run``,
  ``list_runs_for_project``, ``update_run_hidden``, ``delete_run_data``,
  etc.).
- Per-run page queries scoped by ``run_id`` (``count_run_questions``,
  ``get_run_questions_since``) — they're filtered reads, not mutations.
- Budget tracking (``init_budget`` / ``get_budget`` / ``consume_budget``
  / ``add_budget`` / ``budget_remaining``) — the ``budget`` table is
  keyed by run_id.
- Eval-report persistence (AB reports + single-run reports).

Deferred to a later phase:

- ``stage_run`` / ``commit_staged_run`` still live on ``DB`` for now;
  they touch the mutation-event log and will move when
  ``MutationLog`` is hardened into its own capability.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from postgrest.types import CountMethod

from rumil.db.row_helpers import _SLIM_PAGE_COLUMNS, _row_to_call, _row_to_page, _rows
from rumil.models import Call, Page, PageType

if TYPE_CHECKING:
    from rumil.database import DB


class RunStore:
    """Runs + budget + eval reports."""

    def __init__(self, db: "DB") -> None:
        self._db = db

    @property
    def client(self) -> Any:
        return self._db.client

    async def update_run_hidden(self, run_id: str, hidden: bool) -> dict[str, Any] | None:
        """Flip the ``hidden`` flag on a run.

        The RunPicker in the parma UI filters hidden runs out by default; this
        is a soft delete affordance for smoke tests, failed experiments, etc.
        No mutation event is recorded because visibility of run rows is not
        part of the staged-runs model.
        """
        await self._db._execute(
            self.client.table("runs").update({"hidden": hidden}).eq("id", run_id)
        )
        return await self.get_run(run_id)

    async def init_budget(self, total: int) -> None:
        await self._db._execute(
            self.client.table("budget").upsert(
                {
                    "run_id": self._db.run_id,
                    "total": total,
                    "used": 0,
                }
            )
        )

    async def get_budget(self) -> tuple[int, int]:
        """Returns (total, used)."""
        rows = _rows(
            await self._db._execute(
                self.client.table("budget").select("total, used").eq("run_id", self._db.run_id)
            )
        )
        if rows:
            return rows[0]["total"], rows[0]["used"]
        return 0, 0

    async def consume_budget(self, amount: int = 1) -> bool:
        """Deduct from global budget. Returns False if insufficient budget."""
        result = await self._db._execute(
            self.client.rpc(
                "consume_budget",
                {"rid": self._db.run_id, "amount": amount},
            )
        )
        ok = cast(bool, result.data)
        return ok

    async def add_budget(self, amount: int) -> None:
        """Add more calls to the existing budget (for continue runs)."""
        await self._db._execute(
            self.client.rpc(
                "add_budget",
                {"rid": self._db.run_id, "amount": amount},
            )
        )

    async def budget_remaining(self) -> int:
        total, used = await self.get_budget()
        return max(0, total - used)

    async def get_call_rows_for_run(self, run_id: str) -> list[dict]:
        return _rows(
            await self._db._execute(
                self.client.table("calls").select("*").eq("run_id", run_id).order("created_at")
            )
        )

    async def get_calls_for_run(self, run_id: str) -> list[Call]:
        rows = await self.get_call_rows_for_run(run_id)
        return [_row_to_call(r) for r in rows]

    async def get_run_question_id(self, run_id: str) -> str | None:
        rows = _rows(
            await self._db._execute(
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
        page = await self._db.get_page(page_id)
        if not page:
            return None
        if page.provenance_call_id:
            rows = _rows(
                await self._db._execute(
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
            await self._db._execute(
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
        rows = _rows(
            await self._db._execute(self.client.table("runs").select("*").eq("id", run_id))
        )
        return rows[0] if rows else None

    async def create_run(
        self,
        name: str,
        question_id: str | None,
        config: dict | None = None,
        orchestrator: str | None = None,
    ) -> None:
        """Insert a row in the runs table for this DB's run_id.

        If *orchestrator* is provided, it is written into ``config["orchestrator"]``
        so trace-UI consumers can display the canonical orchestrator name.
        """
        final_config = dict(config) if config else {}
        if orchestrator is not None:
            final_config["orchestrator"] = orchestrator
        await self._db._execute(
            self.client.table("runs").insert(
                {
                    "id": self._db.run_id,
                    "name": name,
                    "project_id": self._db.project_id,
                    "question_id": question_id,
                    "config": final_config,
                    "staged": self._db.staged,
                }
            )
        )

    async def get_or_create_named_run(
        self,
        project_id: str,
        name: str,
        config: dict | None = None,
    ) -> str:
        """Return an existing non-staged run id for (project, name), creating one if absent.

        Used by telemetry endpoints (friendly-user flag, read-dwell, etc.) that
        want a stable FK target for reputation_events without creating a fresh
        runs row per event.
        """
        existing = _rows(
            await self._db._execute(
                self.client.table("runs")
                .select("id")
                .eq("project_id", project_id)
                .eq("name", name)
                .eq("staged", False)
                .order("created_at")
                .limit(1)
            )
        )
        if existing:
            return existing[0]["id"]
        new_id = str(uuid.uuid4())
        await self._db._execute(
            self.client.table("runs").insert(
                {
                    "id": new_id,
                    "name": name,
                    "project_id": project_id,
                    "question_id": None,
                    "config": config or {},
                    "staged": False,
                }
            )
        )
        return new_id

    async def count_run_questions(self) -> int:
        """Count question pages created by this run."""
        query = (
            self.client.table("pages")
            .select("id", count=CountMethod.exact)
            .eq("run_id", self._db.run_id)
            .eq("page_type", PageType.QUESTION.value)
        )
        if self._db.project_id:
            query = query.eq("project_id", self._db.project_id)
        query = self._db._staged_filter(query)
        result = await self._db._execute(query)
        return result.count or 0

    async def get_run_questions_since(
        self,
        since: datetime,
    ) -> list[Page]:
        """Return question pages created by this run after *since*."""
        query = (
            self.client.table("pages")
            .select(_SLIM_PAGE_COLUMNS)
            .eq("run_id", self._db.run_id)
            .eq("page_type", PageType.QUESTION.value)
            .gt("created_at", since.isoformat())
            .order("created_at")
        )
        if self._db.project_id:
            query = query.eq("project_id", self._db.project_id)
        query = self._db._staged_filter(query)
        result = await self._db._execute(query)
        pages = [_row_to_page(r) for r in _rows(result)]
        return await self._db._apply_page_events(pages)

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
        await self._db._execute(
            self.client.table("ab_eval_reports").insert(
                {
                    "id": report_id,
                    "run_id_a": run_id_a,
                    "run_id_b": run_id_b,
                    "question_id_a": question_id_a,
                    "question_id_b": question_id_b,
                    "overall_assessment": overall_assessment,
                    "overall_assessment_call_id": overall_assessment_call_id,
                    "dimension_reports": list(dimension_reports),
                    "project_id": str(self._db.project_id) if self._db.project_id else None,
                }
            )
        )
        return report_id

    async def list_ab_eval_reports(self) -> list[dict[str, Any]]:
        """List all AB evaluation reports for this project, newest first."""
        q = (
            self.client.table("ab_eval_reports")
            .select(
                "id, run_id_a, run_id_b, question_id_a, question_id_b, "
                "overall_assessment, dimension_reports, created_at"
            )
            .order("created_at", desc=True)
        )
        if self._db.project_id:
            q = q.eq("project_id", str(self._db.project_id))
        return _rows(await self._db._execute(q))

    async def get_ab_eval_report(self, report_id: str) -> dict[str, Any] | None:
        """Get a single AB evaluation report by ID."""
        q = self.client.table("ab_eval_reports").select("*").eq("id", report_id)
        if self._db.project_id:
            q = q.eq("project_id", str(self._db.project_id))
        rows = _rows(await self._db._execute(q))
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
        await self._db._execute(
            self.client.table("run_eval_reports").insert(
                {
                    "id": report_id,
                    "run_id": run_id,
                    "question_id": question_id,
                    "overall_assessment": overall_assessment,
                    "dimension_reports": list(dimension_reports),
                    "project_id": str(self._db.project_id) if self._db.project_id else None,
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
        if self._db.project_id:
            q = q.eq("project_id", str(self._db.project_id))
        return _rows(await self._db._execute(q))

    async def get_run_eval_report(self, report_id: str) -> dict[str, Any] | None:
        """Get a single run evaluation report by ID."""
        q = self.client.table("run_eval_reports").select("*").eq("id", report_id)
        if self._db.project_id:
            q = q.eq("project_id", str(self._db.project_id))
        rows = _rows(await self._db._execute(q))
        return rows[0] if rows else None

    async def list_runs_for_project(self, project_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """Return recent runs for a project, newest first.

        Queries the runs table and falls back to the calls table for legacy
        runs that predate the runs table.
        """
        run_rows = _rows(
            await self._db._execute(
                self.client.table("runs")
                .select("id, name, question_id, config, created_at, staged, hidden")
                .eq("project_id", project_id)
                .order("created_at", desc=True)
                .limit(limit * 2)
            )
        )
        legacy_rows = _rows(
            await self._db._execute(
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
        pages_by_id = await self._db.get_pages_by_ids(list(page_ids)) if page_ids else {}

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
                    "hidden": row.get("hidden", False),
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

    async def delete_run_data(self, delete_project: bool = False) -> None:
        """Delete all data for this run_id. Used by test teardown."""
        await self._db._execute(
            self.client.table("mutation_events").delete().eq("run_id", self._db.run_id)
        )
        await self._db._execute(
            self.client.table("call_llm_exchanges").delete().eq("run_id", self._db.run_id)
        )
        for table in [
            "page_flags",
            "page_ratings",
            "page_format_events",
            "page_links",
            "reputation_events",
            "annotation_events",
        ]:
            await self._db._execute(self.client.table(table).delete().eq("run_id", self._db.run_id))
        await self._db._execute(
            self.client.table("calls").update({"sequence_id": None}).eq("run_id", self._db.run_id)
        )
        await self._db._execute(
            self.client.table("suggestions").delete().eq("run_id", self._db.run_id)
        )
        await self._db._execute(
            self.client.table("call_sequences").delete().eq("run_id", self._db.run_id)
        )
        for table in ["calls", "pages"]:
            await self._db._execute(self.client.table(table).delete().eq("run_id", self._db.run_id))
        await self._db._execute(self.client.table("budget").delete().eq("run_id", self._db.run_id))
        await self._db._execute(self.client.table("runs").delete().eq("id", self._db.run_id))
        if delete_project and self._db.project_id:
            await self._db._execute(
                self.client.table("projects").delete().eq("id", self._db.project_id)
            )
