"""SUPERSEDE_SPEC_ITEM: replace an existing spec item with a revised one."""

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


class SupersedeSpecItemPayload(BaseModel):
    old_id: str = Field(
        description=(
            "Full or short ID of the spec item being replaced. The old item "
            "is marked superseded; it remains visible in historical artefact "
            "snapshots but no longer contributes to the current spec."
        ),
    )
    headline: str = Field(
        description="Short, sharp label for the revised spec item (10-15 words).",
    )
    content: str = Field(
        description=(
            "The revised spec item: a prescriptive statement about the artefact, "
            "in the same style as add_spec_item — default to a single rule, but "
            "a richer multi-sentence item is fine when motivation or nuance is "
            "essential. Often the right move is to roll a couple of related "
            "smaller items into a single richer one here."
        ),
    )


async def execute(payload: SupersedeSpecItemPayload, call: Call, db: DB) -> MoveResult:
    artefact_task_id = call.scope_page_id
    if not artefact_task_id:
        return MoveResult(
            message=(
                "ERROR: supersede_spec_item requires call.scope_page_id set "
                "to the artefact-task question. No change made."
            ),
            created_page_id=None,
        )
    scope_page = await db.get_page(artefact_task_id)
    if scope_page is None or scope_page.page_type != PageType.QUESTION:
        actual = scope_page.page_type.value if scope_page else "missing"
        return MoveResult(
            message=(
                f"ERROR: supersede_spec_item scope must be a question; got {actual}. "
                "No change made."
            ),
            created_page_id=None,
        )

    old_id = await db.resolve_page_id(payload.old_id)
    if not old_id:
        return MoveResult(
            message=f"ERROR: supersede_spec_item could not resolve old_id={payload.old_id!r}.",
            created_page_id=None,
        )
    old_page = await db.get_page(old_id)
    if old_page is None or old_page.page_type != PageType.SPEC_ITEM or not old_page.is_active():
        return MoveResult(
            message=(
                f"ERROR: supersede_spec_item target {payload.old_id!r} is not an "
                "active spec item. No change made."
            ),
            created_page_id=None,
        )

    old_links = await db.get_links_from(old_id)
    on_this_spec = any(
        l.link_type == LinkType.SPEC_OF and l.to_page_id == artefact_task_id for l in old_links
    )
    if not on_this_spec:
        return MoveResult(
            message=(
                f"ERROR: spec item {payload.old_id!r} is not part of this artefact "
                "task's spec. Supersede the item from the call whose scope owns it."
            ),
            created_page_id=None,
        )

    new_page = Page(
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
    await db.save_page(new_page)
    await db.save_link(
        PageLink(
            from_page_id=new_page.id,
            to_page_id=artefact_task_id,
            link_type=LinkType.SPEC_OF,
        )
    )
    await db.supersede_page(old_id, new_page.id)

    log.info(
        "Spec item superseded: %s -> %s",
        old_id[:8],
        new_page.id[:8],
    )
    return MoveResult(
        message=(
            f"Superseded spec item [{old_id[:8]}] with [{new_page.id[:8]}]: {payload.headline}"
        ),
        created_page_id=new_page.id,
    )


MOVE = MoveDef(
    move_type=MoveType.SUPERSEDE_SPEC_ITEM,
    name="supersede_spec_item",
    description=(
        "Replace an existing spec item with a revised version. The old item "
        "is superseded (remains visible in historical artefact snapshots) "
        "and the new one takes its place in the current spec. Use when you "
        "want to tighten or reshape an existing rule — for outright deletion "
        "with no replacement, use delete_spec_item instead."
    ),
    schema=SupersedeSpecItemPayload,
    execute=execute,
)
