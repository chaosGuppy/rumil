"""CREATE_JUDGEMENT move: create a considered position on a question."""

import logging

from pydantic import Field

from rumil.database import DB
from rumil.models import (
    Call,
    ConsiderationDirection,
    LinkType,
    MoveType,
    PageLayer,
    PageLink,
    PageType,
)
from rumil.moves.base import CreatePagePayload, MoveDef, MoveResult, create_page
from rumil.moves.link_consideration import ConsiderationLinkFields

log = logging.getLogger(__name__)


class CreateJudgementPayload(CreatePagePayload):
    links: list[ConsiderationLinkFields] = Field(
        default_factory=list,
        description=(
            "Question links to create for this judgement. Each entry "
            "links this judgement as a consideration bearing on an "
            "existing question, with direction and strength."
        ),
    )


async def execute(payload: CreateJudgementPayload, call: Call, db: DB) -> MoveResult:
    result = await create_page(payload, call, db, PageType.JUDGEMENT, PageLayer.SQUIDGY)
    if not result.created_page_id or not payload.links:
        return result

    for link_spec in payload.links:
        resolved = await db.resolve_page_id(link_spec.question_id)
        if not resolved:
            log.warning(
                "Inline judgement link skipped: question %s not found",
                link_spec.question_id,
            )
            continue

        direction_str = link_spec.direction.lower()
        try:
            direction = ConsiderationDirection(direction_str)
        except ValueError:
            log.debug(
                "Invalid direction '%s' defaulting to neutral", direction_str,
            )
            direction = ConsiderationDirection.NEUTRAL

        await db.save_link(PageLink(
            from_page_id=result.created_page_id,
            to_page_id=resolved,
            link_type=LinkType.CONSIDERATION,
            direction=direction,
            strength=link_spec.strength,
            reasoning=link_spec.reasoning,
        ))
        log.info(
            "Inline judgement linked: %s -> %s (%s)",
            result.created_page_id[:8], resolved[:8], direction_str,
        )

    return result


MOVE = MoveDef(
    move_type=MoveType.CREATE_JUDGEMENT,
    name="create_judgement",
    description=(
        "Create a judgement — a considered position synthesising the "
        "considerations bearing on a question. Must engage with "
        "considerations on multiple sides. Include key_dependencies and "
        "sensitivity_analysis fields. Use the links field to simultaneously "
        "attach this judgement as a consideration on one or more questions."
    ),
    schema=CreateJudgementPayload,
    execute=execute,
)
