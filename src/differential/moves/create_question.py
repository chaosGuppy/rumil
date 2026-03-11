"""CREATE_QUESTION move: create a research question."""

import logging

from pydantic import BaseModel, Field

from differential.models import LinkType, MoveType, PageLayer, PageLink, PageType
from differential.moves.base import CreatePagePayload, MoveDef, MoveResult, MoveState, create_page

log = logging.getLogger(__name__)


class InlineChildQuestionLink(BaseModel):
    parent_id: str = Field(description="Page ID of the parent question")
    reasoning: str = Field("", description="Why this is a sub-question")


class CreateQuestionPayload(CreatePagePayload):
    links: list[InlineChildQuestionLink] = Field(
        default_factory=list,
        description=(
            "Parent question links to create for this question. Each entry "
            "links an existing question as the parent of this new question."
        ),
    )


async def execute(payload: CreateQuestionPayload, state: MoveState) -> MoveResult:
    result = await create_page(payload, state, PageType.QUESTION, PageLayer.SQUIDGY)
    if not result.created_page_id or not payload.links:
        return result

    db = state.db
    for link_spec in payload.links:
        parent_id = link_spec.parent_id
        if parent_id == "LAST_CREATED" and state.last_created_id:
            parent_id = state.last_created_id
        resolved = await db.resolve_page_id(parent_id)
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
