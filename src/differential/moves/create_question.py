"""CREATE_QUESTION move: create a research question."""

import logging

from pydantic import Field

from differential.database import DB
from differential.models import (
    AssessDispatchPayload,
    BaseDispatchPayload,
    Call,
    CallType,
    Dispatch,
    InlineDispatch,
    LinkType,
    MoveType,
    PageLayer,
    PageLink,
    PageType,
    PrioritizationDispatchPayload,
    ScoutDispatchPayload,
)
from differential.moves.base import CreatePagePayload, MoveDef, MoveResult, create_page
from differential.moves.link_child_question import ChildQuestionLinkFields

log = logging.getLogger(__name__)


class CreateQuestionPayload(CreatePagePayload):
    links: list[ChildQuestionLinkFields] = Field(
        default_factory=list,
        description=(
            "Parent question links to create for this question. Each entry "
            "links an existing question as the parent of this new question."
        ),
    )


async def execute(payload: CreateQuestionPayload, call: Call, db: DB) -> MoveResult:
    result = await create_page(payload, call, db, PageType.QUESTION, PageLayer.SQUIDGY)
    if not result.created_page_id or not payload.links:
        return result

    for link_spec in payload.links:
        resolved = await db.resolve_page_id(link_spec.parent_id)
        if not resolved:
            log.warning(
                "Inline child question link skipped: parent %s not found",
                link_spec.parent_id,
            )
            continue

        await db.save_link(PageLink(
            from_page_id=resolved,
            to_page_id=result.created_page_id,
            link_type=LinkType.CHILD_QUESTION,
            reasoning=link_spec.reasoning,
        ))
        log.info(
            "Inline child question linked: %s -> %s",
            resolved[:8], result.created_page_id[:8],
        )

    return result


class CreateSubquestionPayload(CreateQuestionPayload):
    dispatches: list[InlineDispatch] = Field(
        default_factory=list,
        description=(
            "Research calls to dispatch on this question immediately upon creation. "
            "Each dispatch is queued and executed after prioritization completes."
        ),
    )


_INLINE_DISPATCH_TO_PAYLOAD = {
    "scout": (CallType.SCOUT, ScoutDispatchPayload),
    "assess": (CallType.ASSESS, AssessDispatchPayload),
    "prioritization": (CallType.PRIORITIZATION, PrioritizationDispatchPayload),
}


def _inline_to_dispatch(
    inline: InlineDispatch, question_id: str,
) -> Dispatch:
    call_type, payload_cls = _INLINE_DISPATCH_TO_PAYLOAD[inline.call_type]
    fields = inline.model_dump(exclude={"call_type"})
    fields["question_id"] = question_id
    return Dispatch(call_type=call_type, payload=payload_cls(**fields))


async def execute_subquestion(
    payload: CreateSubquestionPayload, call: Call, db: DB,
) -> MoveResult:
    result = await execute(payload, call, db)
    if not result.created_page_id or not payload.dispatches:
        return result
    dispatches = [
        _inline_to_dispatch(d, result.created_page_id)
        for d in payload.dispatches
    ]
    return MoveResult(
        message=result.message,
        created_page_id=result.created_page_id,
        dispatches=dispatches,
    )


PRIORITIZATION_MOVE = MoveDef(
    move_type=MoveType.CREATE_QUESTION,
    name="create_subquestion",
    description=(
        "Create a new research sub-question and optionally dispatch research "
        "calls on it immediately. Use the dispatches field to queue scout, "
        "assess, or sub-prioritization calls that will execute after "
        "prioritization completes. Use the links field to attach this "
        "question as a child of a parent question."
    ),
    schema=CreateSubquestionPayload,
    execute=execute_subquestion,
)


MOVE = MoveDef(
    move_type=MoveType.CREATE_QUESTION,
    name="create_question",
    description=(
        "Create a new research question — an open problem for investigation. "
        "Use the links field to simultaneously attach this question as a "
        "child of one or more parent questions."
    ),
    schema=CreateQuestionPayload,
    execute=execute,
)
