"""
Supabase database layer for the research workspace.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, cast

from postgrest.types import CountMethod
from supabase import acreate_client, AsyncClient
from supabase.lib.client_options import AsyncClientOptions

from rumil.settings import get_settings
from rumil.models import (
    Call,
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


def _row_to_page(row: dict[str, Any]) -> Page:
    return Page(
        id=row["id"],
        page_type=PageType(row["page_type"]),
        layer=PageLayer(row["layer"]),
        workspace=Workspace(row["workspace"]),
        content=row["content"],
        headline=row["headline"],
        project_id=row.get("project_id") or "",
        epistemic_status=row["epistemic_status"],
        epistemic_type=row["epistemic_type"] or "",
        provenance_model=row["provenance_model"] or "",
        provenance_call_type=row["provenance_call_type"] or "",
        provenance_call_id=row["provenance_call_id"] or "",
        created_at=datetime.fromisoformat(row["created_at"]),
        superseded_by=row["superseded_by"],
        is_superseded=bool(row["is_superseded"]),
        extra=row["extra"] or {},
        abstract=row.get("abstract") or "",
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
    )


class DB:
    def __init__(
        self,
        run_id: str,
        client: AsyncClient,
        project_id: str = "",
        ab_run_id: str | None = None,
    ):
        self.run_id = run_id
        self.client = client
        self.project_id = project_id
        self.ab_run_id = ab_run_id

    @classmethod
    async def create(
        cls,
        run_id: str,
        prod: bool = False,
        project_id: str = "",
        client: AsyncClient | None = None,
        ab_run_id: str | None = None,
    ) -> "DB":
        if client is None:
            url, key = get_settings().get_supabase_credentials(prod)
            client = await acreate_client(
                url, key, options=AsyncClientOptions(schema="public")
            )
        return cls(
            run_id=run_id, client=client, project_id=project_id,
            ab_run_id=ab_run_id,
        )

    def _ab_filter(self, query: Any, table: str = "pages") -> Any:
        """Apply AB run isolation filter to a query."""
        if self.ab_run_id:
            return query.or_(f"ab_run_id.is.null,ab_run_id.eq.{self.ab_run_id}")
        return query.is_("ab_run_id", "null")

    async def get_or_create_project(self, name: str) -> Project:
        rows = _rows(
            await self.client.table("projects").select("*").eq("name", name).execute()
        )
        if rows:
            row = rows[0]
            return Project(
                id=row["id"],
                name=row["name"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
        row = _rows(
            await self.client.table("projects")
            .insert({"name": name})
            .execute()
        )[0]
        return Project(
            id=row["id"],
            name=row["name"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    async def list_projects(self) -> list[Project]:
        rows = _rows(
            await self.client.table("projects")
            .select("*")
            .order("created_at")
            .execute()
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
        await self.client.table("pages").upsert(
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
                "provenance_model": page.provenance_model,
                "provenance_call_type": page.provenance_call_type,
                "provenance_call_id": page.provenance_call_id,
                "created_at": page.created_at.isoformat(),
                "superseded_by": page.superseded_by,
                "is_superseded": page.is_superseded,
                "extra": page.extra,
                "run_id": self.run_id,
                "ab_run_id": self.ab_run_id,
                "abstract": page.abstract,
            }
        ).execute()

    async def update_page_summaries(
        self, page_id: str, headline: str, abstract: str
    ) -> None:
        await self.client.table("pages").update(
            {"headline": headline, "abstract": abstract}
        ).eq("id", page_id).execute()

    async def get_page(self, page_id: str) -> Page | None:
        rows = _rows(
            await self.client.table("pages").select("*").eq("id", page_id).execute()
        )
        return _row_to_page(rows[0]) if rows else None

    async def resolve_page_id(self, page_id: str) -> str | None:
        """Resolve a page ID to a full UUID. Handles both full UUIDs and
        8-char short IDs. Returns the full UUID if found, or None."""
        if not page_id:
            log.debug("resolve_page_id: empty page_id")
            return None
        # Try exact match first
        rows = _rows(
            await self.client.table("pages").select("id").eq("id", page_id).execute()
        )
        if rows:
            log.debug("resolve_page_id: exact match for %s", page_id[:8])
            return rows[0]["id"]
        # Try prefix match for short IDs
        if len(page_id) <= 8:
            rows = _rows(
                await self.client.table("pages")
                .select("id")
                .like("id", f"{page_id}%")
                .execute()
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
        log.debug("resolve_page_id: no match for %s", page_id[:8])
        return None

    async def page_label(self, page_id: str) -> str:
        """Return a human-readable label like '"Summary text" [short_id]'."""
        page = await self.get_page(page_id)
        if page:
            return f'"{page.headline[:60]}" [{page_id[:8]}]'
        return f"[{page_id[:8]}]"

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
        query = self._ab_filter(query)
        return [
            _row_to_page(r)
            for r in _rows(
                await query.order("created_at", desc=True).limit(10000).execute()
            )
        ]

    async def supersede_page(self, old_id: str, new_id: str) -> None:
        await self.client.table("pages").update(
            {
                "is_superseded": True,
                "superseded_by": new_id,
            }
        ).eq("id", old_id).execute()

    # --- Links ---

    async def save_link(self, link: PageLink) -> None:
        log.debug(
            "save_link: %s -> %s, type=%s",
            link.from_page_id[:8], link.to_page_id[:8], link.link_type.value,
        )
        await self.client.table("page_links").upsert(
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
                "ab_run_id": self.ab_run_id,
            }
        ).execute()

    async def get_link(self, link_id: str) -> PageLink | None:
        rows = _rows(
            await self.client.table("page_links")
            .select("*")
            .eq("id", link_id)
            .execute()
        )
        return _row_to_link(rows[0]) if rows else None

    async def get_links_to(self, page_id: str) -> list[PageLink]:
        query = self.client.table("page_links").select("*").eq("to_page_id", page_id)
        query = self._ab_filter(query, table="page_links")
        rows = _rows(await query.execute())
        return [_row_to_link(r) for r in rows]

    async def get_links_from(self, page_id: str) -> list[PageLink]:
        query = self.client.table("page_links").select("*").eq("from_page_id", page_id)
        query = self._ab_filter(query, table="page_links")
        rows = _rows(await query.execute())
        return [_row_to_link(r) for r in rows]

    async def get_latest_summary_for_question(self, question_id: str) -> "Page | None":
        """Return the most recent active SUMMARY page linked to a question."""
        links = await self.get_links_to(question_id)
        summary_links = [l for l in links if l.link_type == LinkType.SUMMARIZES]
        candidates = []
        for link in summary_links:
            page = await self.get_page(link.from_page_id)
            if page and page.is_active() and page.page_type == PageType.SUMMARY:
                candidates.append(page)
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
        result = []
        for link in consideration_links:
            page = await self.get_page(link.from_page_id)
            if page and page.is_active():
                result.append((page, link))
        return result

    async def get_child_questions(self, parent_id: str) -> list[Page]:
        """Return sub-questions of a question."""
        links = await self.get_links_from(parent_id)
        child_links = [l for l in links if l.link_type == LinkType.CHILD_QUESTION]
        result = []
        for link in child_links:
            page = await self.get_page(link.to_page_id)
            if page and page.is_active():
                result.append(page)
        return result

    async def get_child_questions_with_links(
        self, parent_id: str,
    ) -> list[tuple[Page, PageLink]]:
        """Return (child_page, link) pairs for sub-questions of a question."""
        links = await self.get_links_from(parent_id)
        child_links = [l for l in links if l.link_type == LinkType.CHILD_QUESTION]
        result = []
        for link in child_links:
            page = await self.get_page(link.to_page_id)
            if page and page.is_active():
                result.append((page, link))
        return result

    async def get_judgements_for_question(self, question_id: str) -> list[Page]:
        links = await self.get_links_to(question_id)
        judgement_links = [l for l in links if l.link_type == LinkType.RELATED]
        result = []
        for link in judgement_links:
            page = await self.get_page(link.from_page_id)
            if page and page.is_active() and page.page_type == PageType.JUDGEMENT:
                result.append(page)
        return result

    # --- Calls ---

    async def create_call(
        self,
        call_type: CallType,
        scope_page_id: str | None = None,
        parent_call_id: str | None = None,
        budget_allocated: int | None = None,
        workspace: Workspace = Workspace.RESEARCH,
        context_page_ids: list | None = None,
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
        )
        await self.save_call(call)
        return call

    async def save_call(self, call: Call) -> None:
        if not call.project_id:
            call.project_id = self.project_id
        await self.client.table("calls").upsert(
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
            }
        ).execute()

    async def get_call(self, call_id: str) -> Call | None:
        rows = _rows(
            await self.client.table("calls").select("*").eq("id", call_id).execute()
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
        await self.client.table("calls").update(
            payload
        ).eq("id", call_id).execute()

    async def increment_call_budget_used(
        self,
        call_id: str,
        amount: int = 1,
    ) -> None:
        await self.client.rpc(
            "increment_call_budget_used",
            {"call_id": call_id, "amount": amount},
        ).execute()

    # --- Per-run budget ---

    async def init_budget(self, total: int) -> None:
        await self.client.table("budget").upsert(
            {
                "run_id": self.run_id,
                "total": total,
                "used": 0,
            }
        ).execute()

    async def get_budget(self) -> tuple[int, int]:
        """Returns (total, used)."""
        rows = _rows(
            await self.client.table("budget")
            .select("total, used")
            .eq("run_id", self.run_id)
            .execute()
        )
        if rows:
            return rows[0]["total"], rows[0]["used"]
        return 0, 0

    async def consume_budget(self, amount: int = 1) -> bool:
        """Deduct from global budget. Returns False if insufficient budget."""
        result = await self.client.rpc(
            "consume_budget",
            {"rid": self.run_id, "amount": amount},
        ).execute()
        ok = cast(bool, result.data)
        log.debug("consume_budget: amount=%d, success=%s", amount, ok)
        return ok

    async def add_budget(self, amount: int) -> None:
        """Add more calls to the existing budget (for continue runs)."""
        await self.client.rpc(
            "add_budget",
            {"rid": self.run_id, "amount": amount},
        ).execute()

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
        query = self._ab_filter(query, table="page_links")
        rows = _rows(await query.execute())
        return [_row_to_link(r) for r in rows]

    async def get_all_links(self) -> list[PageLink]:
        """Bulk-fetch all links, optionally scoped by project via page membership."""
        rows = _rows(
            await self.client.table("page_links")
            .select("*")
            .limit(50000)
            .execute()
        )
        return [_row_to_link(r) for r in rows]

    async def delete_link(self, link_id: str) -> None:
        """Delete a page link by ID."""
        await self.client.table("page_links").delete().eq("id", link_id).execute()

    async def update_link_role(self, link_id: str, role: LinkRole) -> None:
        """Update a link's role."""
        await self.client.table("page_links").update(
            {"role": role.value}
        ).eq("id", link_id).execute()

    async def get_last_scout_info(
        self,
        question_id: str,
    ) -> tuple[str, int | None] | None:
        """Return (completed_at_iso, remaining_fruit) for the most recent
        scout call on this question, or None if never scouted."""
        rows = _rows(
            await self.client.table("calls")
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

    async def get_ingest_history(self) -> dict[str, list[str]]:
        """Return {source_id: [question_id, ...]} based on considerations
        created by ingest calls."""
        params: dict[str, Any] = {}
        if self.project_id:
            params["pid"] = self.project_id
        rows = _rows(await self.client.rpc("get_ingest_history", params).execute())
        out: dict[str, list[str]] = {}
        for row in rows:
            out.setdefault(row["source_id"], []).append(row["question_id"])
        return out

    # --- Traces ---

    async def save_call_trace(self, call_id: str, events: list[dict]) -> None:
        """Append trace events to the call's trace_json column."""
        await self.client.rpc(
            "append_call_trace",
            {"cid": call_id, "new_events": events},
        ).execute()

    async def get_call_trace(self, call_id: str) -> list[dict]:
        """Fetch trace events for a call."""
        rows = _rows(
            await self.client.table("calls")
            .select("trace_json")
            .eq("id", call_id)
            .execute()
        )
        if rows and rows[0].get("trace_json"):
            return rows[0]["trace_json"]
        return []

    async def get_child_calls(self, parent_call_id: str) -> list[Call]:
        """Fetch direct child calls ordered by created_at."""
        rows = _rows(
            await self.client.table("calls")
            .select("*")
            .eq("parent_call_id", parent_call_id)
            .order("created_at")
            .execute()
        )
        return [_row_to_call(r) for r in rows]

    async def get_root_calls_for_question(self, question_id: str) -> list[Call]:
        """Find top-level calls for a question (prioritization calls with no
        parent, or whose parent targets a different question)."""
        rows = _rows(
            await self.client.table("calls")
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
            await self.client.table("calls")
            .select("*")
            .eq("scope_page_id", question_id)
            .order("created_at")
            .execute()
        )
        return [_row_to_call(r) for r in rows]

    async def save_page_rating(
        self,
        page_id: str,
        call_id: str,
        score: int,
        note: str = "",
    ) -> None:
        await self.client.table("page_ratings").insert(
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

    async def save_page_flag(
        self,
        flag_type: str,
        call_id: str | None = None,
        note: str = "",
        page_id: str | None = None,
        page_id_a: str | None = None,
        page_id_b: str | None = None,
    ) -> None:
        await self.client.table("page_flags").insert(
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

    async def get_root_questions(
        self,
        workspace: Workspace = Workspace.RESEARCH,
    ) -> list[Page]:
        """Return questions that have no parent (top-level questions)."""
        params: dict[str, Any] = {"ws": workspace.value}
        if self.project_id:
            params["pid"] = self.project_id
        if self.ab_run_id:
            params["p_ab_run_id"] = self.ab_run_id
        rows = _rows(
            await self.client.rpc("get_root_questions", params).execute()
        )
        return [_row_to_page(r) for r in rows]

    async def count_pages_for_question(self, question_id: str) -> dict:
        """Count pages linked to or created in context of a question."""
        cons_result = await (
            self.client.table("page_links")
            .select("id", count=CountMethod.exact)
            .eq("to_page_id", question_id)
            .eq("link_type", "consideration")
            .execute()
        )
        judgements_result = await self.client.rpc(
            "count_active_judgements",
            {"qid": question_id},
        ).execute()
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
        await self.client.table("call_llm_exchanges").insert(row).execute()
        return exchange_id

    async def get_llm_exchanges(self, call_id: str) -> list[dict[str, Any]]:
        rows = _rows(
            await self.client.table("call_llm_exchanges")
            .select("id, call_id, phase, round, input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens, duration_ms, error, created_at")
            .eq("call_id", call_id)
            .order("round")
            .execute()
        )
        return rows

    async def get_llm_exchange(self, exchange_id: str) -> dict[str, Any] | None:
        rows = _rows(
            await self.client.table("call_llm_exchanges")
            .select("*")
            .eq("id", exchange_id)
            .execute()
        )
        return rows[0] if rows else None

    async def get_calls_for_run(self, run_id: str) -> list[Call]:
        rows = _rows(
            await self.client.table("calls")
            .select("*")
            .eq("run_id", run_id)
            .order("created_at")
            .execute()
        )
        return [_row_to_call(r) for r in rows]

    async def get_run_question_id(self, run_id: str) -> str | None:
        rows = _rows(
            await self.client.table("calls")
            .select("scope_page_id")
            .eq("run_id", run_id)
            .is_("parent_call_id", "null")
            .order("created_at")
            .limit(1)
            .execute()
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
                await self.client.table("calls")
                .select("run_id, created_at")
                .eq("id", page.provenance_call_id)
                .limit(1)
                .execute()
            )
            if rows and rows[0].get("run_id"):
                return {
                    "run_id": rows[0]["run_id"],
                    "created_at": rows[0]["created_at"],
                }
        rows = _rows(
            await self.client.table("calls")
            .select("run_id, created_at")
            .eq("scope_page_id", page_id)
            .is_("parent_call_id", "null")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
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
        await self.client.table("runs").insert(
            {
                "id": self.run_id,
                "name": name,
                "project_id": self.project_id,
                "question_id": question_id,
                "config": config or {},
                "ab_run_id": self.ab_run_id,
                "ab_arm": ab_arm,
            }
        ).execute()

    async def create_ab_run(
        self,
        ab_run_id: str,
        name: str,
        question_id: str | None,
    ) -> None:
        """Insert a row in the ab_runs table."""
        await self.client.table("ab_runs").insert(
            {
                "id": ab_run_id,
                "name": name,
                "project_id": self.project_id,
                "question_id": question_id,
            }
        ).execute()

    async def list_runs_for_project(self, project_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """Return recent runs for a project, newest first.

        Queries the runs table. Groups AB runs into a single entry with
        both arm run_ids. Falls back to the calls table for legacy runs
        that predate the runs table.
        """
        run_rows = _rows(
            await self.client.table("runs")
            .select("id, name, question_id, config, ab_run_id, ab_arm, created_at")
            .eq("project_id", project_id)
            .order("created_at", desc=True)
            .limit(limit * 2)
            .execute()
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
            await self.client.table("calls")
            .select("run_id, created_at, scope_page_id")
            .eq("project_id", project_id)
            .is_("parent_call_id", "null")
            .order("created_at", desc=True)
            .execute()
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
        await self.client.table("call_llm_exchanges").delete().eq(
            "run_id", self.run_id
        ).execute()
        for table in ["page_flags", "page_ratings", "page_links", "calls", "pages"]:
            await self.client.table(table).delete().eq("run_id", self.run_id).execute()
        await self.client.table("budget").delete().eq("run_id", self.run_id).execute()
        await self.client.table("runs").delete().eq("id", self.run_id).execute()
        if self.ab_run_id:
            # Only delete ab_run if no other runs reference it
            remaining = _rows(
                await self.client.table("runs")
                .select("id")
                .eq("ab_run_id", self.ab_run_id)
                .limit(1)
                .execute()
            )
            if not remaining:
                await self.client.table("ab_runs").delete().eq(
                    "id", self.ab_run_id
                ).execute()
        if delete_project and self.project_id:
            await self.client.table("projects").delete().eq(
                "id", self.project_id
            ).execute()
