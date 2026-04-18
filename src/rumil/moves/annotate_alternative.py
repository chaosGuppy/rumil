"""ANNOTATE_ALTERNATIVE move: propose a counterfactual action for a trace step.

Adversarial-review wrappers and post-hoc reviewers use this to point at a
specific step in a completed call's trace and say 'should have fired X with
params Y because Z'. See marketplace-thread/28-annotation-primitives.md.
"""

import logging

from pydantic import BaseModel, Field

from rumil.database import DB
from rumil.models import Call, MoveType
from rumil.moves.base import MoveDef, MoveResult

log = logging.getLogger(__name__)


class AnnotateAlternativePayload(BaseModel):
    target_call_id: str = Field(description="Call ID whose trace event is being annotated.")
    target_event_seq: int = Field(
        description=(
            "Index into the target call's trace_json events array. 0 is the "
            "first event; pick the index of the step you disagree with."
        ),
    )
    alternative: str = Field(
        description=(
            "What should have happened instead. Name a concrete call type, "
            "move, or target if possible (e.g. 'fire web_research on the "
            "parent question' or 'call assess on claim C3 first')."
        ),
    )
    rationale: str = Field(
        description=(
            "Why the alternative would have been better. Keep it grounded in "
            "what the model should have known at that point in the trace."
        ),
    )


async def execute(payload: AnnotateAlternativePayload, call: Call, db: DB) -> MoveResult:
    await db.record_annotation(
        annotation_type="counterfactual_tool_use",
        author_type="model",
        author_id=call.id,
        target_call_id=payload.target_call_id,
        target_event_seq=payload.target_event_seq,
        note=payload.rationale,
        payload={
            "alternative": payload.alternative,
            "rationale": payload.rationale,
        },
    )
    try:
        await db.record_reputation_event(
            source="model_annotation",
            dimension="counterfactual_tool_use",
            score=1.0,
            source_call_id=call.id,
            extra={
                "target_call_id": payload.target_call_id,
                "target_event_seq": payload.target_event_seq,
            },
        )
    except Exception:
        log.exception(
            "Failed to mirror counterfactual annotation into reputation_events (non-fatal)"
        )
    log.info(
        "Counterfactual annotation: call=%s event=%d alt=%s",
        payload.target_call_id[:8],
        payload.target_event_seq,
        payload.alternative[:80],
    )
    return MoveResult("Annotation recorded.")


MOVE = MoveDef(
    move_type=MoveType.ANNOTATE_ALTERNATIVE,
    name="annotate_alternative",
    description=(
        "Point at a specific step in a completed call's trace and propose a "
        "counterfactual: 'this call fired X at step N; it should have fired Y "
        "because Z'. target_call_id is the call being reviewed; "
        "target_event_seq is the 0-indexed position of the trace event you "
        "disagree with. Use this to make adversarial review structured rather "
        "than prose critiques no downstream code reads."
    ),
    schema=AnnotateAlternativePayload,
    execute=execute,
)
