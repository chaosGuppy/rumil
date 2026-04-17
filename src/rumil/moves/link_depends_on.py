"""LINK_DEPENDS_ON move: declare that a page depends on another page being true/valid."""

import logging

from pydantic import BaseModel, Field

from rumil.database import DB
from rumil.models import Call, LinkType, MoveType, PageLink, PageType
from rumil.moves.base import MoveDef, MoveResult

log = logging.getLogger(__name__)

_DEPENDS_ON_TYPES = (PageType.CLAIM, PageType.JUDGEMENT)


class LinkDependsOnPayload(BaseModel):
    dependent_page_id: str = Field(
        description=(
            "Page ID of the page that depends on another (or LAST_CREATED). "
            "Must be a claim or judgement."
        )
    )
    dependency_page_id: str = Field(
        description=(
            "Page ID of the load-bearing page being depended on. Must be a "
            "claim or judgement — never a question. If you mean 'depends on "
            "the answer to this question', point at the question's current "
            "judgement instead."
        )
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
            dependent_id,
            dependency_id,
        )
        return MoveResult("Link skipped — page IDs not found.")

    dependent_page = await db.get_page(dependent_id)
    dependency_page = await db.get_page(dependency_id)
    if dependent_page is None or dependent_page.page_type not in _DEPENDS_ON_TYPES:
        log.warning(
            "LINK_DEPENDS_ON skipped: dependent %s is %s, expected claim/judgement",
            dependent_id[:8],
            dependent_page.page_type.value if dependent_page else "missing",
        )
        return MoveResult(
            "Link skipped — depends_on links must originate from a claim or judgement. "
            "Use link_child_question to relate questions to each other."
        )
    if dependency_page is None or dependency_page.page_type not in _DEPENDS_ON_TYPES:
        kind = dependency_page.page_type.value if dependency_page else "missing"
        log.warning(
            "LINK_DEPENDS_ON skipped: dependency %s is %s, expected claim/judgement",
            dependency_id[:8],
            kind,
        )
        if dependency_page is not None and dependency_page.page_type == PageType.QUESTION:
            return MoveResult(
                "Link skipped — depends_on must point at a claim or judgement, not a "
                "question. If you mean 'depends on the answer to this question', link "
                "to the question's current judgement instead."
            )
        return MoveResult("Link skipped — depends_on must point at a claim or judgement.")

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
        dependent_id[:8],
        dependency_id[:8],
        payload.strength,
    )
    return MoveResult("Done.")


MOVE = MoveDef(
    move_type=MoveType.LINK_DEPENDS_ON,
    name="link_depends_on",
    description=(
        "Declare that a claim or judgement depends on another claim or "
        "judgement being true or valid. Both endpoints must be a claim or "
        "judgement — questions are never valid endpoints. If a claim or "
        "judgement depends on the answer to a question, point at that "
        "question's current judgement instead. Use after creating a claim "
        "that builds on another claim, or after creating a judgement to "
        "record which considerations were most load-bearing for the "
        "conclusion."
    ),
    schema=LinkDependsOnPayload,
    execute=execute,
)
