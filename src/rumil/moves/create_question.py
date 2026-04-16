"""CREATE_QUESTION move: create a research question."""

import logging

from pydantic import Field

from rumil.database import DB
from rumil.models import (
    Call,
    LinkType,
    MoveType,
    PageLayer,
    PageLink,
    PageType,
)
from rumil.moves.base import CreatePagePayload, MoveDef, MoveResult, create_page
from rumil.moves.link_child_question import ChildQuestionLinkFields

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

        await db.save_link(
            PageLink(
                from_page_id=resolved,
                to_page_id=result.created_page_id,
                link_type=LinkType.CHILD_QUESTION,
                reasoning=link_spec.reasoning,
                role=link_spec.role,
                impact_on_parent_question=link_spec.impact_on_parent_question,
            )
        )
        log.info(
            "Inline child question linked: %s -> %s",
            resolved[:8],
            result.created_page_id[:8],
        )

    return result


async def execute_scout_question(
    payload: CreatePagePayload,
    call: Call,
    db: DB,
) -> MoveResult:
    """Create a question and auto-link it as a child of the call's scope question."""
    result = await create_page(payload, call, db, PageType.QUESTION, PageLayer.SQUIDGY)
    if not result.created_page_id or not call.scope_page_id:
        return result

    await db.save_link(
        PageLink(
            from_page_id=call.scope_page_id,
            to_page_id=result.created_page_id,
            link_type=LinkType.CHILD_QUESTION,
            reasoning="Auto-linked to scope question",
        )
    )
    log.info(
        "Scout question auto-linked: %s -> %s",
        call.scope_page_id[:8],
        result.created_page_id[:8],
    )
    return result


SCOUT_MOVE = MoveDef(
    move_type=MoveType.CREATE_SCOUT_QUESTION,
    name="create_question",
    description=(
        "Create a new research question — an open problem for investigation. "
        "The question is automatically linked as a child of the scope question."
    ),
    schema=CreatePagePayload,
    execute=execute_scout_question,
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
