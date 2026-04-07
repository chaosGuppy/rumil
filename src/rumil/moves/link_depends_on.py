"""LINK_DEPENDS_ON move: declare that a page depends on another page being true/valid."""

import logging

from pydantic import BaseModel, Field

from rumil.database import DB
from rumil.models import Call, LinkType, MoveType, PageLink
from rumil.moves.base import MoveDef, MoveResult

log = logging.getLogger(__name__)


class LinkDependsOnPayload(BaseModel):
    dependent_page_id: str = Field(
        description="Page ID of the page that depends on another (or LAST_CREATED)"
    )
    dependency_page_id: str = Field(
        description="Page ID of the load-bearing page being depended on"
    )
    strength: float = Field(
        3.0,
        description=(
            "1-5: how load-bearing the dependency is "
            "(1 = mildly depends on, 5 = would collapse without it)"
        ),
    )
    reasoning: str = Field(
        "",
        description="Why this dependency is load-bearing",
    )


async def execute(payload: LinkDependsOnPayload, call: Call, db: DB) -> MoveResult:
    dependent_id = await db.resolve_page_id(payload.dependent_page_id)
    dependency_id = await db.resolve_page_id(payload.dependency_page_id)
    if not dependent_id or not dependency_id:
        log.warning(
            "LINK_DEPENDS_ON skipped: dependent=%s, dependency=%s",
            dependent_id, dependency_id,
        )
        return MoveResult("Link skipped — page IDs not found.")

    link = PageLink(
        from_page_id=dependent_id,
        to_page_id=dependency_id,
        link_type=LinkType.DEPENDS_ON,
        strength=payload.strength,
        reasoning=payload.reasoning,
    )
    await db.save_link(link)
    log.info(
        "Dependency linked: %s depends on %s (%.1f)",
        dependent_id[:8], dependency_id[:8], payload.strength,
    )
    return MoveResult("Done.")


MOVE = MoveDef(
    move_type=MoveType.LINK_DEPENDS_ON,
    name="link_depends_on",
    description=(
        "Declare that a page depends on another page being true or valid. "
        "Use after creating a claim that builds on another claim, or after "
        "creating a judgement to record which considerations were most "
        "load-bearing for the conclusion."
    ),
    schema=LinkDependsOnPayload,
    execute=execute,
)
