"""ADD_SPEC_ITEM move: add a prescriptive spec item bearing on the artefact task."""

import logging

from pydantic import BaseModel, Field

from rumil.database import DB
from rumil.models import (
    Call,
    LinkType,
    MoveType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)
from rumil.moves.base import MoveDef, MoveResult
from rumil.settings import get_settings

log = logging.getLogger(__name__)


class AddSpecItemPayload(BaseModel):
    headline: str = Field(
        description=(
            "Short, sharp label for this spec item — 10-15 words, self-contained. "
            "Names the prescriptive rule the artefact should satisfy."
        ),
    )
    content: str = Field(
        description=(
            "The spec item itself: one precise prescriptive statement about the "
            "artefact. Prefer atomic rules over bundles — if you're saying two "
            "independent things, make two spec items. Write in terms of what the "
            "artefact should or should not do, include, or look like."
        ),
    )


async def execute(payload: AddSpecItemPayload, call: Call, db: DB) -> MoveResult:
    artefact_task_id = call.scope_page_id
    if not artefact_task_id:
        return MoveResult(
            message=(
                "ERROR: add_spec_item requires the call's scope_page_id to be set "
                "to the artefact-task question. No spec item was created."
            ),
            created_page_id=None,
        )

    page = Page(
        page_type=PageType.SPEC_ITEM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=payload.content,
        headline=payload.headline,
        hidden=True,
        provenance_model=get_settings().model,
        provenance_call_type=call.call_type.value,
        provenance_call_id=call.id,
    )
    await db.save_page(page)
    await db.save_link(
        PageLink(
            from_page_id=page.id,
            to_page_id=artefact_task_id,
            link_type=LinkType.SPEC_OF,
        )
    )
    log.info(
        "Spec item added: %s -> artefact task %s",
        page.id[:8],
        artefact_task_id[:8],
    )
    return MoveResult(
        message=f"Added spec item [{page.id[:8]}]: {payload.headline}",
        created_page_id=page.id,
    )


MOVE = MoveDef(
    move_type=MoveType.ADD_SPEC_ITEM,
    name="add_spec_item",
    description=(
        "Add one atomic prescriptive spec item for the artefact the generative "
        "workflow will produce. A spec item says what the artefact should or "
        "should not do, include, or look like — a rule the generator will be "
        "held to. Prefer sharp, individually-falsifiable rules over broad "
        "bundles. The spec item is linked to the artefact-task question "
        "automatically; you do not need to reference it in the payload."
    ),
    schema=AddSpecItemPayload,
    execute=execute,
)
