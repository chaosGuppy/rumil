"""ANNOTATE_SPAN move: pin structured feedback to a page character range.

Models (scouts, review calls, big assess) use this to emit a structured
annotation on a specific span of a page, instead of burying the critique in
prose. See marketplace-thread/28-annotation-primitives.md.
"""

import logging

from pydantic import BaseModel, Field

from rumil.database import DB
from rumil.models import Call, MoveType
from rumil.moves.base import MoveDef, MoveResult

log = logging.getLogger(__name__)


class AnnotateSpanPayload(BaseModel):
    target_page_id: str = Field(description="Page ID of the page being annotated.")
    span_start: int = Field(
        description="Character offset of the start of the span (0-indexed, inclusive).",
    )
    span_end: int = Field(
        description="Character offset of the end of the span (exclusive).",
    )
    note: str = Field(
        description=(
            "What you want to say about this span. Be specific — this should "
            "read as useful feedback to someone improving this page or the "
            "prompt that produced it."
        ),
    )
    category: str | None = Field(
        default=None,
        description=(
            "Optional coarse bucket, e.g. 'factual_error', 'unsupported', "
            "'missing_consideration', 'scope_confused', 'praise'."
        ),
    )


async def execute(payload: AnnotateSpanPayload, call: Call, db: DB) -> MoveResult:
    target = await db.resolve_page_id(payload.target_page_id) or payload.target_page_id
    await db.record_annotation(
        annotation_type="span",
        author_type="model",
        author_id=call.id,
        target_page_id=target,
        span_start=payload.span_start,
        span_end=payload.span_end,
        category=payload.category,
        note=payload.note,
    )
    try:
        await db.record_reputation_event(
            source="model_annotation",
            dimension="span",
            score=1.0,
            source_call_id=call.id,
            extra={
                "target_page_id": target,
                "category": payload.category,
            },
        )
    except Exception:
        log.exception("Failed to mirror span annotation into reputation_events (non-fatal)")
    log.info(
        "Span annotation: page=%s, range=[%d,%d), category=%s, note=%s",
        target[:8] if target else "?",
        payload.span_start,
        payload.span_end,
        payload.category,
        payload.note[:80],
    )
    return MoveResult("Annotation recorded.")


MOVE = MoveDef(
    move_type=MoveType.ANNOTATE_SPAN,
    name="annotate_span",
    description=(
        "Pin a structured annotation to a character range on a page. Use this "
        "instead of burying feedback in prose — e.g. 'this clause overreaches', "
        "'the citation doesn't support this sentence', 'this paragraph is the "
        "strongest part of the claim'. Provide span_start/span_end as "
        "character offsets into the page content. Keep notes specific and "
        "useful to someone improving the page."
    ),
    schema=AnnotateSpanPayload,
    execute=execute,
)
