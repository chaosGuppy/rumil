"""CallStore: calls, call_sequences, call_llm_exchanges, traces.

Owns everything keyed by ``call_id``: the call itself, its lifecycle
transitions, its batching via ``CallSequence``, its per-exchange LLM
records, and its JSONB ``trace_json`` column.

Calls do not participate in the staged-runs mutation-log contract — the
``calls`` table has no ``staged`` column today. ``get_recent_calls_for_question``
documents this explicitly. A future extension would need both a schema
change and event replay (see CLAUDE.md).

``get_latest_judgement_for_call`` queries the ``pages`` table but lives
here because the query is semantically "what did this call produce?" —
keeping it next to the call lifecycle rather than in PageStore.
"""

import hashlib
import logging
import re
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from rumil.db.row_helpers import _row_to_call, _row_to_call_sequence, _rows
from rumil.models import Call, CallSequence, CallStatus, CallType, PageType, Workspace

if TYPE_CHECKING:
    from rumil.database import DB


log = logging.getLogger(__name__)


_DATE_SUFFIX_RE = re.compile(r"\n\nIMPORTANT: Today's date is \d{4}-\d{2}-\d{2}\n?$")


def _strip_date_suffix(prompt: str) -> str:
    """Remove the daily date suffix appended by ``_with_date_suffix`` in llm.py.

    Hashing must be stable across days, so strip the suffix before computing
    the content hash. If the suffix isn't present (legacy prompts, odd
    whitespace), the original string is returned.
    """
    return _DATE_SUFFIX_RE.sub("", prompt)


def _hash_prompt_content(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


_PROMPT_HASH_UPSERT_CACHE: set[str] = set()


class CallStore:
    """Calls + sequences + LLM exchanges + traces."""

    def __init__(self, db: "DB") -> None:
        self._db = db

    @property
    def client(self) -> Any:
        return self._db.client

    async def resolve_call_id(self, call_id: str) -> str | None:
        """Resolve a call ID to a full UUID. Handles both full UUIDs and
        8-char short IDs. Returns the full UUID if found, or None."""
        if not call_id:
            return None
        rows = _rows(
            await self._db._execute(self.client.table("calls").select("id").eq("id", call_id))
        )
        if rows:
            return rows[0]["id"]
        if len(call_id) <= 8:
            rows = _rows(
                await self._db._execute(
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
        )
        if call_id is not None:
            call.id = call_id
        await self.save_call(call)
        return call

    async def save_call(self, call: Call) -> None:
        if not call.project_id:
            call.project_id = self._db.project_id
        await self._db._execute(
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
                    "run_id": self._db.run_id,
                    "sequence_id": call.sequence_id,
                    "sequence_position": call.sequence_position,
                    "cost_usd": call.cost_usd,
                }
            )
        )

    async def get_call(self, call_id: str) -> Call | None:
        rows = _rows(
            await self._db._execute(self.client.table("calls").select("*").eq("id", call_id))
        )
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
        if status == CallStatus.COMPLETE:
            primary = await self._resolve_primary_prompt(call_id)
            if primary is not None:
                payload["primary_prompt_hash"] = primary[0]
                payload["primary_prompt_name"] = primary[1]
        await self._db._execute(self.client.table("calls").update(payload).eq("id", call_id))

    async def _resolve_primary_prompt(self, call_id: str) -> tuple[str, str] | None:
        """Pick the call's primary (hash, name) from its exchanges.

        Prefers the first non-closing-review exchange's ``composite_prompt_hash``.
        Falls back to the first exchange of any phase. Returns None if no
        exchange has a hash stamped (e.g. call ran before prompt versioning
        landed, or system_prompt was empty).
        """
        rows = _rows(
            await self._db._execute(
                self.client.table("call_llm_exchanges")
                .select("phase,composite_prompt_hash,round")
                .eq("call_id", call_id)
                .not_.is_("composite_prompt_hash", "null")
                .order("round", desc=False)
            )
        )
        if not rows:
            return None
        agent_rows = [r for r in rows if not str(r.get("phase", "")).startswith("closing_review")]
        chosen = agent_rows[0] if agent_rows else rows[0]
        call_rows = _rows(
            await self._db._execute(
                self.client.table("calls").select("call_type").eq("id", call_id).limit(1)
            )
        )
        name = call_rows[0]["call_type"] if call_rows else "unknown"
        return chosen["composite_prompt_hash"], name

    async def increment_call_budget_used(
        self,
        call_id: str,
        amount: int = 1,
    ) -> None:
        await self._db._execute(
            self.client.rpc(
                "increment_call_budget_used",
                {"call_id": call_id, "amount": amount},
            )
        )

    async def get_last_find_considerations_info(
        self,
        question_id: str,
    ) -> tuple[str, int | None] | None:
        """Return (completed_at_iso, remaining_fruit) for the most recent
        find_considerations call on this question, or None if never run."""
        rows = _rows(
            await self._db._execute(
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
            await self._db._execute(
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
            await self._db._execute(
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
        if self._db.project_id:
            params["pid"] = self._db.project_id
        rows = _rows(await self._db._execute(self.client.rpc("get_ingest_history", params)))
        out: dict[str, list[str]] = {}
        for row in rows:
            out.setdefault(row["source_id"], []).append(row["question_id"])
        return out

    async def save_call_trace(self, call_id: str, events: Sequence[dict]) -> None:
        """Append trace events to the call's trace_json column."""
        await self._db._execute(
            self.client.rpc(
                "append_call_trace",
                {"cid": call_id, "new_events": events},
            )
        )

    async def get_call_trace(self, call_id: str) -> list[dict]:
        """Fetch trace events for a call."""
        rows = _rows(
            await self._db._execute(
                self.client.table("calls").select("trace_json").eq("id", call_id)
            )
        )
        if rows and rows[0].get("trace_json"):
            return rows[0]["trace_json"]
        return []

    async def get_child_calls(self, parent_call_id: str) -> list[Call]:
        """Fetch direct child calls ordered by created_at."""
        rows = _rows(
            await self._db._execute(
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
            run_id=self._db.run_id,
            scope_question_id=scope_question_id,
            position_in_batch=position_in_batch,
        )
        await self._db._execute(
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
            await self._db._execute(
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
            await self._db._execute(
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
            await self._db._execute(
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
        rows = _rows(
            await self._db._execute(
                self.client.table("calls")
                .select("*")
                .eq("scope_page_id", question_id)
                .order("created_at")
            )
        )
        return [_row_to_call(r) for r in rows]

    async def get_recent_calls_for_question(
        self,
        question_id: str,
        limit: int = 10,
    ) -> list[Call]:
        """Return the most recent completed calls scoped to a question, newest first."""
        query = (
            self.client.table("calls")
            .select("*")
            .eq("scope_page_id", question_id)
            .eq("status", CallStatus.COMPLETE.value)
        )
        if self._db.project_id:
            query = query.eq("project_id", self._db.project_id)
        rows = _rows(await self._db._execute(query.order("created_at", desc=True).limit(limit)))
        return [_row_to_call(r) for r in rows]

    async def get_latest_judgement_for_call(
        self,
        call_id: str,
    ) -> str | None:
        """Return the page ID of the most recent judgement created by a call."""
        rows = _rows(
            await self._db._execute(
                self.client.table("pages")
                .select("id")
                .eq("provenance_call_id", call_id)
                .eq("page_type", PageType.JUDGEMENT.value)
                .order("created_at", desc=True)
                .limit(1)
            )
        )
        return rows[0]["id"] if rows else None

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
        prompt_name: str = "composite",
    ) -> str:
        exchange_id = str(uuid.uuid4())
        composite_hash: str | None = None
        if system_prompt:
            pre_suffix = _strip_date_suffix(system_prompt)
            hash_candidate = _hash_prompt_content(pre_suffix)
            composite_hash = hash_candidate
            if hash_candidate not in _PROMPT_HASH_UPSERT_CACHE:
                try:
                    await self._db._execute(
                        self.client.rpc(
                            "upsert_prompt_version",
                            {
                                "p_hash": hash_candidate,
                                "p_name": prompt_name,
                                "p_content": pre_suffix,
                                "p_kind": "composite",
                            },
                        )
                    )
                    _PROMPT_HASH_UPSERT_CACHE.add(hash_candidate)
                except Exception as e:
                    log.warning(
                        "upsert_prompt_version failed (hash=%s): %s", hash_candidate[:12], e
                    )
                    composite_hash = None
        row: dict[str, Any] = {
            "id": exchange_id,
            "call_id": call_id,
            "run_id": self._db.run_id,
            "staged": self._db.staged,
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
            "composite_prompt_hash": composite_hash,
        }
        if user_messages is not None:
            row["user_messages"] = user_messages
        await self._db._execute(self.client.table("call_llm_exchanges").insert(row))
        return exchange_id

    async def get_llm_exchanges(self, call_id: str) -> list[dict[str, Any]]:
        rows = _rows(
            await self._db._execute(
                self.client.table("call_llm_exchanges")
                .select(
                    "id, call_id, phase, round, input_tokens, output_tokens, "
                    "cache_creation_input_tokens, cache_read_input_tokens, "
                    "duration_ms, error, created_at"
                )
                .eq("call_id", call_id)
                .order("round")
            )
        )
        return rows

    async def get_llm_exchange(self, exchange_id: str) -> dict[str, Any] | None:
        rows = _rows(
            await self._db._execute(
                self.client.table("call_llm_exchanges").select("*").eq("id", exchange_id)
            )
        )
        return rows[0] if rows else None
