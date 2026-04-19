"""AnnotationStore: ratings, flags, reputation events, annotations,
epistemic scores, page-format events.

Everything here is append-only (from the AnnotationStore's perspective) —
these are signals collected about pages/calls, not mutations to them.
Each table carries ``staged`` and ``run_id`` so staged runs see baseline
rows plus their own, exactly like ``reputation_events`` does today (see
"Staged Runs and the Mutation Log" in CLAUDE.md).

``save_epistemic_score`` is the one method that crosses into mutation-log
territory: credence/robustness live as ``set_credence`` / ``set_robustness``
events on the mutation log (since migration
``20260418152516_fold_epistemic_scores_into_mutation_events``). We still
co-locate the entry point here because the caller's mental model is
"record an epistemic annotation on this page". The underlying event is
recorded via ``self._db.record_mutation_event``.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from rumil.db.eval_summary import EvalSummary, aggregate_eval_rows_by_subject
from rumil.db.row_helpers import _row_to_annotation_event, _row_to_page, _rows
from rumil.models import AnnotationEvent, Page, PageType, ReputationEvent

if TYPE_CHECKING:
    from rumil.database import DB


class AnnotationStore:
    """Ratings + flags + reputation + annotations + epistemic scores."""

    def __init__(self, db: "DB") -> None:
        self._db = db

    @property
    def client(self) -> Any:
        return self._db.client

    async def save_page_rating(
        self,
        page_id: str,
        call_id: str,
        score: int,
        note: str = "",
    ) -> None:
        await self._db._execute(
            self.client.table("page_ratings").insert(
                {
                    "id": str(uuid.uuid4()),
                    "page_id": page_id,
                    "call_id": call_id,
                    "score": score,
                    "note": note,
                    "created_at": datetime.now(UTC).isoformat(),
                    "run_id": self._db.run_id,
                    "staged": self._db.staged,
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
        await self._db._execute(
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
                    "run_id": self._db.run_id,
                    "staged": self._db.staged,
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

        Writes staged=self._db.staged and run_id=self._db.run_id so staged
        runs are isolated from baseline readers. Never aggregate or
        normalize at this layer — callers keep each (source, dimension) raw.
        """
        row: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "run_id": self._db.run_id,
            "project_id": self._db.project_id,
            "source": source,
            "dimension": dimension,
            "score": score,
            "orchestrator": orchestrator,
            "task_shape": task_shape,
            "source_call_id": source_call_id,
            "extra": extra or {},
            "staged": self._db.staged,
            "created_at": datetime.now(UTC).isoformat(),
        }
        await self._db._execute(self.client.table("reputation_events").insert(row))

    async def get_reputation_events(
        self,
        *,
        run_id: str | None = None,
        source: str | None = None,
        dimension: str | None = None,
        orchestrator: str | None = None,
    ) -> list[ReputationEvent]:
        """Fetch reputation events, respecting the staged-visibility rule."""
        query = self.client.table("reputation_events").select("*")
        if self._db.project_id:
            query = query.eq("project_id", self._db.project_id)
        if run_id is not None:
            query = query.eq("run_id", run_id)
        if source is not None:
            query = query.eq("source", source)
        if dimension is not None:
            query = query.eq("dimension", dimension)
        if orchestrator is not None:
            query = query.eq("orchestrator", orchestrator)
        query = self._db._staged_filter(query)
        rows = _rows(await self._db._execute(query))
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
        """Group reputation events by (source, dimension, orchestrator)."""
        query = self.client.table("reputation_events").select("*").eq("project_id", project_id)
        if orchestrator is not None:
            query = query.eq("orchestrator", orchestrator)
        if source is not None:
            query = query.eq("source", source)
        if dimension is not None:
            query = query.eq("dimension", dimension)
        query = self._db._staged_filter(query)
        rows = _rows(await self._db._execute(query))

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
    ) -> dict[str, dict[str, EvalSummary]]:
        """Batched eval summary per page per dimension."""
        if not page_ids or not dimensions:
            return {}
        query = (
            self.client.table("reputation_events")
            .select("dimension,score,created_at,extra")
            .in_("extra->>subject_page_id", list(page_ids))
            .in_("dimension", list(dimensions))
        )
        if self._db.project_id:
            query = query.eq("project_id", self._db.project_id)
        query = self._db._staged_filter(query)
        rows = _rows(await self._db._execute(query))
        return aggregate_eval_rows_by_subject(rows, subject_key="subject_page_id")

    async def get_eval_summary_for_calls(
        self,
        call_ids: Sequence[str],
        dimensions: Sequence[str],
    ) -> dict[str, dict[str, EvalSummary]]:
        """Batched eval summary per call per dimension."""
        if not call_ids or not dimensions:
            return {}
        query = (
            self.client.table("reputation_events")
            .select("dimension,score,created_at,extra")
            .in_("extra->>subject_call_id", list(call_ids))
            .in_("dimension", list(dimensions))
        )
        if self._db.project_id:
            query = query.eq("project_id", self._db.project_id)
        query = self._db._staged_filter(query)
        rows = _rows(await self._db._execute(query))
        return aggregate_eval_rows_by_subject(rows, subject_key="subject_call_id")

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
        """Append a raw annotation signal."""
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
            run_id=self._db.run_id,
            project_id=self._db.project_id,
            staged=self._db.staged,
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
        await self._db._execute(self.client.table("annotation_events").insert(row))
        return ev

    async def get_annotations(
        self,
        *,
        target_page_id: str | None = None,
        target_call_id: str | None = None,
        author_type: str | None = None,
        annotation_type: str | None = None,
    ) -> list[AnnotationEvent]:
        """Fetch annotations, respecting the staged-visibility rule."""
        query = self.client.table("annotation_events").select("*")
        if self._db.project_id:
            query = query.eq("project_id", self._db.project_id)
        if target_page_id is not None:
            query = query.eq("target_page_id", target_page_id)
        if target_call_id is not None:
            query = query.eq("target_call_id", target_call_id)
        if author_type is not None:
            query = query.eq("author_type", author_type)
        if annotation_type is not None:
            query = query.eq("annotation_type", annotation_type)
        query = self._db._staged_filter(query)
        rows = _rows(await self._db._execute(query))
        return [_row_to_annotation_event(r) for r in rows]

    async def get_annotations_by_target_pages(
        self,
        page_ids: Sequence[str],
    ) -> dict[str, list[AnnotationEvent]]:
        """Batched annotation fetch: one query for many target pages."""
        result: dict[str, list[AnnotationEvent]] = {pid: [] for pid in page_ids}
        if not page_ids:
            return result
        query = (
            self.client.table("annotation_events").select("*").in_("target_page_id", list(page_ids))
        )
        if self._db.project_id:
            query = query.eq("project_id", self._db.project_id)
        query = self._db._staged_filter(query)
        rows = _rows(await self._db._execute(query))
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
        Each score is recorded as a ``set_credence`` or ``set_robustness``
        event, and non-staged runs also dual-write the score + reasoning
        directly onto the ``pages`` row so other readers see the update
        immediately. Supply at least one of ``credence`` / ``robustness``.
        """
        if credence is None and robustness is None:
            raise ValueError("save_epistemic_score requires at least one of credence or robustness")
        existing = await self._db.get_page(page_id)
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
            await self._db.record_mutation_event("set_credence", page_id, payload)
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
            await self._db.record_mutation_event("set_robustness", page_id, payload)
            page_updates["robustness"] = int(robustness)
            page_updates["robustness_reasoning"] = reasoning
        if page_updates and not self._db.staged:
            await self._db._execute(
                self.client.table("pages").update(page_updates).eq("id", page_id)
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
                "run_id": self._db.run_id,
                "staged": self._db.staged,
                "tags": e.get("tags", {}),
            }
            for e in events
        ]
        await self._db._execute(self.client.table("page_format_events").insert(rows))

    async def get_page_format_events_for_run(self, run_id: str) -> Sequence[dict[str, Any]]:
        """Fetch all page-format events for a run, with call_type from calls."""
        rows = _rows(
            await self._db._execute(
                self.client.table("page_format_events")
                .select("page_id,detail,call_id,tags")
                .eq("run_id", run_id)
            )
        )
        if not rows:
            return []
        call_ids = list({r["call_id"] for r in rows})
        call_rows = _rows(
            await self._db._execute(
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
        """Return the latest epistemic score entry and its source judgement (if any)."""
        rows = _rows(
            await self._db._execute(
                self.client.table("mutation_events")
                .select("event_type, payload, created_at")
                .eq("target_id", page_id)
                .eq("run_id", self._db.run_id)
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
            await self._db._execute(
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
