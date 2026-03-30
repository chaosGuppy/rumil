"""PROMOTE_CONCEPT move: promote a validated concept from staging to research workspace."""

import logging

from pydantic import BaseModel, Field

from rumil.database import DB
from rumil.models import Call, MoveType, Page, PageLayer, PageType, Workspace
from rumil.moves.base import (
    MoveDef,
    MoveResult,
    extract_and_link_citations,
    write_page_file,
)

log = logging.getLogger(__name__)


class PromoteConceptPayload(BaseModel):
    concept_page_id: str = Field(
        description="Full or short ID of the staged concept page to promote."
    )
    reasoning: str = Field(
        "",
        description="Why this concept has earned a place in the research workspace.",
    )


async def execute(payload: PromoteConceptPayload, call: Call, db: DB) -> MoveResult:
    resolved = await db.resolve_page_id(payload.concept_page_id)
    if not resolved:
        return MoveResult(f"Concept page '{payload.concept_page_id}' not found.")

    staging_page = await db.get_page(resolved)
    if not staging_page:
        return MoveResult(f"Concept page '{resolved[:8]}' not found.")

    if staging_page.workspace != Workspace.CONCEPT_STAGING:
        return MoveResult(
            f"Page '{resolved[:8]}' is not a staged concept "
            f"(workspace: {staging_page.workspace.value})."
        )

    research_page = Page(
        page_type=PageType.CONCEPT,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=staging_page.content,
        headline=staging_page.headline,
        credence=staging_page.credence,
        robustness=staging_page.robustness,
        provenance_model="claude-opus-4-6",
        provenance_call_type=call.call_type.value,
        provenance_call_id=call.id,
        extra={
            **staging_page.extra,
            "promoted": True,
            "promoted_from": resolved,
            "promotion_reasoning": payload.reasoning,
        },
    )
    await db.save_page(research_page)
    write_page_file(research_page)
    try:
        await extract_and_link_citations(
            research_page.id,
            research_page.content,
            db,
        )
    except Exception:
        log.warning(
            "Citation extraction failed for page %s",
            research_page.id[:8],
            exc_info=True,
        )
    await db.supersede_page(resolved, research_page.id)

    log.info(
        "Concept promoted: staging=%s -> research=%s, headline=%s",
        resolved[:8],
        research_page.id[:8],
        research_page.headline[:60],
    )
    return MoveResult(
        message=(
            f"Promoted concept [{research_page.id[:8]}]: {research_page.headline}. "
            f"Now active in research workspace."
        ),
        created_page_id=research_page.id,
    )


MOVE = MoveDef(
    move_type=MoveType.PROMOTE_CONCEPT,
    name="promote_concept",
    description=(
        "Promote a validated staged concept into the research workspace, making it "
        "visible to all future research calls. Only call this when you are confident "
        "the concept genuinely clarifies the research."
    ),
    schema=PromoteConceptPayload,
    execute=execute,
)
