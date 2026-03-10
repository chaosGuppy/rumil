"""
Supabase database layer for the research workspace.
"""

import os
import uuid
from datetime import datetime, timezone
from typing import Any, cast

from postgrest.types import CountMethod
from supabase import create_client, Client
from supabase.lib.client_options import SyncClientOptions

from differential.models import (
    Call,
    CallStatus,
    CallType,
    ConsiderationDirection,
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)

# Supabase SDK types APIResponse.data as JSON | None, but table queries
# always return list[dict]. We cast to this alias for clarity.
_Rows = list[dict[str, Any]]


def _rows(response: Any) -> _Rows:
    """Extract rows from a Supabase API response with proper typing."""
    return cast(_Rows, response.data) if response.data else []


# Default local Supabase credentials (from `supabase status`)
_DEFAULT_URL = "http://127.0.0.1:54321"
_DEFAULT_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZS1kZW1vIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImV4cCI6MTk4MzgxMjk5Nn0."
    "EGIM96RAZx35lJzdJsyH-qQwv8Hdp7fsn3W0YpN81IU"
)


def _make_client(prod: bool = False) -> Client:
    if prod:
        url = os.environ["SUPABASE_PROD_URL"]
        key = os.environ["SUPABASE_PROD_KEY"]
    else:
        url = os.environ.get("SUPABASE_URL", _DEFAULT_URL)
        key = os.environ.get("SUPABASE_KEY", _DEFAULT_KEY)
    return create_client(url, key, options=SyncClientOptions(schema="public"))


def _row_to_page(row: dict[str, Any]) -> Page:
    return Page(
        id=row["id"],
        page_type=PageType(row["page_type"]),
        layer=PageLayer(row["layer"]),
        workspace=Workspace(row["workspace"]),
        content=row["content"],
        summary=row["summary"],
        epistemic_status=row["epistemic_status"],
        epistemic_type=row["epistemic_type"] or "",
        provenance_model=row["provenance_model"] or "",
        provenance_call_type=row["provenance_call_type"] or "",
        provenance_call_id=row["provenance_call_id"] or "",
        created_at=datetime.fromisoformat(row["created_at"]),
        superseded_by=row["superseded_by"],
        is_superseded=bool(row["is_superseded"]),
        extra=row["extra"] or {},
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
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _row_to_call(row: dict[str, Any]) -> Call:
    return Call(
        id=row["id"],
        call_type=CallType(row["call_type"]),
        workspace=Workspace(row["workspace"]),
        status=CallStatus(row["status"]),
        parent_call_id=row["parent_call_id"],
        scope_page_id=row["scope_page_id"],
        budget_allocated=row["budget_allocated"],
        budget_used=row["budget_used"],
        context_page_ids=row.get("context_page_ids") or [],
        result_summary=row.get("result_summary") or "",
        review_json=row.get("review_json") or {},
        created_at=datetime.fromisoformat(row["created_at"]),
        completed_at=(
            datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None
        ),
    )


class DB:
    def __init__(
        self,
        run_id: str,
        client: Client | None = None,
        prod: bool = False,
    ):
        self.run_id = run_id
        self.client = client or _make_client(prod=prod)

    # --- Pages ---

    def save_page(self, page: Page) -> None:
        self.client.table("pages").upsert(
            {
                "id": page.id,
                "page_type": page.page_type.value,
                "layer": page.layer.value,
                "workspace": page.workspace.value,
                "content": page.content,
                "summary": page.summary,
                "epistemic_status": page.epistemic_status,
                "epistemic_type": page.epistemic_type,
                "provenance_model": page.provenance_model,
                "provenance_call_type": page.provenance_call_type,
                "provenance_call_id": page.provenance_call_id,
                "created_at": page.created_at.isoformat(),
                "superseded_by": page.superseded_by,
                "is_superseded": page.is_superseded,
                "extra": page.extra,
                "run_id": self.run_id,
            }
        ).execute()

    def get_page(self, page_id: str) -> Page | None:
        rows = _rows(self.client.table("pages").select("*").eq("id", page_id).execute())
        return _row_to_page(rows[0]) if rows else None

    def resolve_page_id(self, page_id: str) -> str | None:
        """Resolve a page ID to a full UUID. Handles both full UUIDs and
        8-char short IDs. Returns the full UUID if found, or None."""
        if not page_id:
            return None
        # Try exact match first
        rows = _rows(
            self.client.table("pages").select("id").eq("id", page_id).execute()
        )
        if rows:
            return rows[0]["id"]
        # Try prefix match for short IDs
        if len(page_id) <= 8:
            rows = _rows(
                self.client.table("pages")
                .select("id")
                .like("id", f"{page_id}%")
                .execute()
            )
            if len(rows) == 1:
                return rows[0]["id"]
            if len(rows) > 1:
                print(
                    f"  [db] Ambiguous short ID '{page_id}' matches "
                    f"{len(rows)} pages — skipping."
                )
            return None
        return None

    def page_label(self, page_id: str) -> str:
        """Return a human-readable label like '"Summary text" [short_id]'."""
        page = self.get_page(page_id)
        if page:
            return f'"{page.summary[:60]}" [{page_id[:8]}]'
        return f"[{page_id[:8]}]"

    def get_pages(
        self,
        workspace: Workspace | None = None,
        page_type: PageType | None = None,
        active_only: bool = True,
    ) -> list[Page]:
        query = self.client.table("pages").select("*")
        if workspace:
            query = query.eq("workspace", workspace.value)
        if page_type:
            query = query.eq("page_type", page_type.value)
        if active_only:
            query = query.eq("is_superseded", False)
        return [
            _row_to_page(r)
            for r in _rows(query.order("created_at", desc=True).execute())
        ]

    def supersede_page(self, old_id: str, new_id: str) -> None:
        self.client.table("pages").update(
            {
                "is_superseded": True,
                "superseded_by": new_id,
            }
        ).eq("id", old_id).execute()

    # --- Links ---

    def save_link(self, link: PageLink) -> None:
        self.client.table("page_links").upsert(
            {
                "id": link.id,
                "from_page_id": link.from_page_id,
                "to_page_id": link.to_page_id,
                "link_type": link.link_type.value,
                "direction": link.direction.value if link.direction else None,
                "strength": link.strength,
                "reasoning": link.reasoning,
                "created_at": link.created_at.isoformat(),
                "run_id": self.run_id,
            }
        ).execute()

    def get_links_to(self, page_id: str) -> list[PageLink]:
        rows = _rows(
            self.client.table("page_links")
            .select("*")
            .eq("to_page_id", page_id)
            .execute()
        )
        return [_row_to_link(r) for r in rows]

    def get_links_from(self, page_id: str) -> list[PageLink]:
        rows = _rows(
            self.client.table("page_links")
            .select("*")
            .eq("from_page_id", page_id)
            .execute()
        )
        return [_row_to_link(r) for r in rows]

    def get_considerations_for_question(
        self,
        question_id: str,
    ) -> list[tuple[Page, PageLink]]:
        """Return (claim_page, link) pairs for all considerations on a question."""
        links = self.get_links_to(question_id)
        consideration_links = [
            l for l in links if l.link_type == LinkType.CONSIDERATION
        ]
        result = []
        for link in consideration_links:
            page = self.get_page(link.from_page_id)
            if page and page.is_active():
                result.append((page, link))
        return result

    def get_child_questions(self, parent_id: str) -> list[Page]:
        """Return sub-questions of a question."""
        links = self.get_links_from(parent_id)
        child_links = [l for l in links if l.link_type == LinkType.CHILD_QUESTION]
        result = []
        for link in child_links:
            page = self.get_page(link.to_page_id)
            if page and page.is_active():
                result.append(page)
        return result

    def get_judgements_for_question(self, question_id: str) -> list[Page]:
        links = self.get_links_to(question_id)
        judgement_links = [l for l in links if l.link_type == LinkType.RELATED]
        result = []
        for link in judgement_links:
            page = self.get_page(link.from_page_id)
            if page and page.is_active() and page.page_type == PageType.JUDGEMENT:
                result.append(page)
        return result

    # --- Calls ---

    def create_call(
        self,
        call_type: CallType,
        scope_page_id: str | None = None,
        parent_call_id: str | None = None,
        budget_allocated: int | None = None,
        workspace: Workspace = Workspace.RESEARCH,
        context_page_ids: list | None = None,
    ) -> Call:
        call = Call(
            call_type=call_type,
            workspace=workspace,
            scope_page_id=scope_page_id,
            parent_call_id=parent_call_id,
            budget_allocated=budget_allocated,
            status=CallStatus.PENDING,
            context_page_ids=context_page_ids or [],
        )
        self.save_call(call)
        return call

    def save_call(self, call: Call) -> None:
        self.client.table("calls").upsert(
            {
                "id": call.id,
                "call_type": call.call_type.value,
                "workspace": call.workspace.value,
                "status": call.status.value,
                "parent_call_id": call.parent_call_id,
                "scope_page_id": call.scope_page_id,
                "budget_allocated": call.budget_allocated,
                "budget_used": call.budget_used,
                "context_page_ids": call.context_page_ids,
                "result_summary": call.result_summary,
                "review_json": call.review_json,
                "created_at": call.created_at.isoformat(),
                "completed_at": (
                    call.completed_at.isoformat() if call.completed_at else None
                ),
                "run_id": self.run_id,
            }
        ).execute()

    def get_call(self, call_id: str) -> Call | None:
        rows = _rows(self.client.table("calls").select("*").eq("id", call_id).execute())
        return _row_to_call(rows[0]) if rows else None

    def update_call_status(
        self,
        call_id: str,
        status: CallStatus,
        result_summary: str = "",
    ) -> None:
        completed_at = (
            datetime.now(timezone.utc).isoformat()
            if status == CallStatus.COMPLETE
            else None
        )
        self.client.table("calls").update(
            {
                "status": status.value,
                "result_summary": result_summary,
                "completed_at": completed_at,
            }
        ).eq("id", call_id).execute()

    def increment_call_budget_used(
        self,
        call_id: str,
        amount: int = 1,
    ) -> None:
        self.client.rpc(
            "increment_call_budget_used",
            {"call_id": call_id, "amount": amount},
        ).execute()

    # --- Per-run budget ---

    def init_budget(self, total: int) -> None:
        self.client.table("budget").upsert(
            {
                "run_id": self.run_id,
                "total": total,
                "used": 0,
            }
        ).execute()

    def get_budget(self) -> tuple[int, int]:
        """Returns (total, used)."""
        rows = _rows(
            self.client.table("budget")
            .select("total, used")
            .eq("run_id", self.run_id)
            .execute()
        )
        if rows:
            return rows[0]["total"], rows[0]["used"]
        return 0, 0

    def consume_budget(self, amount: int = 1) -> bool:
        """Deduct from global budget. Returns False if insufficient budget."""
        result = self.client.rpc(
            "consume_budget",
            {"rid": self.run_id, "amount": amount},
        ).execute()
        return cast(bool, result.data)

    def add_budget(self, amount: int) -> None:
        """Add more calls to the existing budget (for continue runs)."""
        self.client.rpc(
            "add_budget",
            {"rid": self.run_id, "amount": amount},
        ).execute()

    def budget_remaining(self) -> int:
        total, used = self.get_budget()
        return max(0, total - used)

    def get_last_scout_info(
        self,
        question_id: str,
    ) -> tuple[str, int | None] | None:
        """Return (completed_at_iso, remaining_fruit) for the most recent
        scout call on this question, or None if never scouted."""
        rows = _rows(
            self.client.table("calls")
            .select("completed_at, review_json")
            .eq("call_type", CallType.SCOUT.value)
            .eq("scope_page_id", question_id)
            .eq("status", "complete")
            .order("completed_at", desc=True)
            .limit(1)
            .execute()
        )
        if not rows or not rows[0]["completed_at"]:
            return None
        row = rows[0]
        review = row["review_json"] or {}
        fruit = review.get("remaining_fruit") if isinstance(review, dict) else None
        return row["completed_at"], fruit

    def get_ingest_history(self) -> dict[str, list[str]]:
        """Return {source_id: [question_id, ...]} based on considerations
        created by ingest calls."""
        rows = _rows(self.client.rpc("get_ingest_history").execute())
        out: dict[str, list[str]] = {}
        for row in rows:
            out.setdefault(row["source_id"], []).append(row["question_id"])
        return out

    # --- Traces ---

    def save_call_trace(self, call_id: str, events: list[dict]) -> None:
        """Append trace events to the call's trace_json column."""
        self.client.rpc(
            "append_call_trace",
            {"cid": call_id, "new_events": events},
        ).execute()

    def get_call_trace(self, call_id: str) -> list[dict]:
        """Fetch trace events for a call."""
        rows = _rows(
            self.client.table("calls").select("trace_json").eq("id", call_id).execute()
        )
        if rows and rows[0].get("trace_json"):
            return rows[0]["trace_json"]
        return []

    def get_child_calls(self, parent_call_id: str) -> list[Call]:
        """Fetch direct child calls ordered by created_at."""
        rows = _rows(
            self.client.table("calls")
            .select("*")
            .eq("parent_call_id", parent_call_id)
            .order("created_at")
            .execute()
        )
        return [_row_to_call(r) for r in rows]

    def get_root_calls_for_question(self, question_id: str) -> list[Call]:
        """Find top-level calls for a question (prioritization calls with no
        parent, or whose parent targets a different question)."""
        rows = _rows(
            self.client.table("calls")
            .select("*")
            .eq("scope_page_id", question_id)
            .is_("parent_call_id", "null")
            .order("created_at")
            .execute()
        )
        result = [_row_to_call(r) for r in rows]
        if result:
            return result
        # Fallback: return all calls scoped to this question
        rows = _rows(
            self.client.table("calls")
            .select("*")
            .eq("scope_page_id", question_id)
            .order("created_at")
            .execute()
        )
        return [_row_to_call(r) for r in rows]

    def save_page_rating(
        self,
        page_id: str,
        call_id: str,
        score: int,
        note: str = "",
    ) -> None:
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
        ).execute()

    def save_page_flag(
        self,
        flag_type: str,
        call_id: str | None = None,
        note: str = "",
        page_id: str | None = None,
        page_id_a: str | None = None,
        page_id_b: str | None = None,
    ) -> None:
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
        ).execute()

    def get_root_questions(
        self,
        workspace: Workspace = Workspace.RESEARCH,
    ) -> list[Page]:
        """Return questions that have no parent (top-level questions)."""
        rows = _rows(
            self.client.rpc(
                "get_root_questions",
                {"ws": workspace.value},
            ).execute()
        )
        return [_row_to_page(r) for r in rows]

    def count_pages_for_question(self, question_id: str) -> dict:
        """Count pages linked to or created in context of a question."""
        cons_result = (
            self.client.table("page_links")
            .select("id", count=CountMethod.exact)
            .eq("to_page_id", question_id)
            .eq("link_type", "consideration")
            .execute()
        )
        judgements_result = self.client.rpc(
            "count_active_judgements",
            {"qid": question_id},
        ).execute()
        return {
            "considerations": cons_result.count or 0,
            "judgements": cast(int, judgements_result.data or 0),
        }

    def delete_run_data(self) -> None:
        """Delete all data for this run_id. Used by test teardown."""
        for table in ["page_flags", "page_ratings", "page_links", "calls", "pages"]:
            self.client.table(table).delete().eq("run_id", self.run_id).execute()
        self.client.table("budget").delete().eq("run_id", self.run_id).execute()
