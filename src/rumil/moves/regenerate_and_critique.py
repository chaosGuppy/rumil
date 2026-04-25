"""REGENERATE_AND_CRITIQUE: fire generate_artefact + critique_artefact in sequence.

Called from the refine_spec agent loop. Regenerating without a fresh critique,
or critiquing without a fresh artefact, would desynchronise the signal the
refiner reads — so the two sub-calls are bundled into a single atomic tool.
"""

import logging

from pydantic import BaseModel, Field

from rumil.database import DB
from rumil.models import Call, CallType, LinkType, MoveType, Page, PageType
from rumil.moves.base import MoveDef, MoveResult

log = logging.getLogger(__name__)


class RegenerateAndCritiquePayload(BaseModel):
    reason: str = Field(
        default="",
        description=(
            "Optional one-sentence note on what the spec change was meant to "
            "fix. Stored for audit only; the critic does not see it."
        ),
    )


async def execute(payload: RegenerateAndCritiquePayload, call: Call, db: DB) -> MoveResult:
    # Deferred imports: moves.registry loads every MoveDef at import time,
    # which transitively pulls in closing_reviewers → moves.registry. Keeping
    # the CallRunner imports inside execute() breaks the cycle.
    from rumil.calls.critique_artefact import CritiqueArtefactCall
    from rumil.calls.critique_artefact_request_only import RequestOnlyCritiqueArtefactCall
    from rumil.calls.generate_artefact import GenerateArtefactCall
    from rumil.tracing.tracer import get_trace

    parent_trace = get_trace()
    broadcaster = parent_trace.broadcaster if parent_trace is not None else None

    artefact_task_id = call.scope_page_id
    if not artefact_task_id:
        return MoveResult(
            message=(
                "ERROR: regenerate_and_critique requires call.scope_page_id "
                "set to the artefact-task question. No sub-calls fired."
            ),
            created_page_id=None,
        )
    scope_page = await db.get_page(artefact_task_id)
    if scope_page is None or scope_page.page_type != PageType.QUESTION:
        actual = scope_page.page_type.value if scope_page else "missing"
        return MoveResult(
            message=(f"ERROR: regenerate_and_critique scope must be a question; got {actual}."),
            created_page_id=None,
        )

    if not await db.consume_budget(3):
        return MoveResult(
            message=(
                "ERROR: regenerate_and_critique needs 3 units of budget (one "
                "for the artefact, two for the workspace-aware and "
                "request-only critiques) and fewer are available. No sub-calls "
                "fired. Consider calling finalize_artefact to ship the latest "
                "artefact instead."
            ),
            created_page_id=None,
        )

    gen_call = await db.create_call(
        CallType.GENERATE_ARTEFACT,
        scope_page_id=artefact_task_id,
        parent_call_id=call.id,
    )
    await GenerateArtefactCall(artefact_task_id, gen_call, db, broadcaster=broadcaster).run()

    crit_call = await db.create_call(
        CallType.CRITIQUE_ARTEFACT,
        scope_page_id=artefact_task_id,
        parent_call_id=call.id,
    )
    await CritiqueArtefactCall(artefact_task_id, crit_call, db, broadcaster=broadcaster).run()

    crit_ro_call = await db.create_call(
        CallType.CRITIQUE_ARTEFACT_REQUEST_ONLY,
        scope_page_id=artefact_task_id,
        parent_call_id=call.id,
    )
    await RequestOnlyCritiqueArtefactCall(
        artefact_task_id, crit_ro_call, db, broadcaster=broadcaster
    ).run()

    new_artefact = await db.latest_artefact_for_task(artefact_task_id)
    if new_artefact is None:
        return MoveResult(
            message="ERROR: after regenerate_and_critique, no artefact exists for the task.",
            created_page_id=None,
        )

    critiques = await _critiques_for_artefact(new_artefact.id, db)
    summary_parts = [
        f"Regenerated artefact [{new_artefact.id[:8]}]: {new_artefact.headline}",
    ]
    grades: dict[str, int] = {}
    for kind, page in critiques.items():
        grade = page.extra.get("grade") if page else None
        issues = page.extra.get("issues") if page else []
        if grade is not None:
            grades[kind] = grade
        label = (
            "Workspace-aware critique"
            if kind == CallType.CRITIQUE_ARTEFACT.value
            else "Request-only critique"
        )
        if grade is not None:
            summary_parts.append(f"{label} grade: {grade}/10")
        if issues:
            summary_parts.append(f"{label} issues:")
            summary_parts.append("\n".join(f"- {issue}" for issue in issues))
    summary = "\n\n".join(summary_parts)

    log.info(
        "regenerate_and_critique complete: artefact=%s, grades=%s",
        new_artefact.id[:8],
        grades,
    )
    return MoveResult(
        message=summary,
        created_page_id=None,
        trace_extra={
            "artefact_id": new_artefact.id,
            "grades": grades,
        },
    )


async def _critiques_for_artefact(artefact_id: str, db: DB) -> dict[str, Page]:
    """Return the latest workspace-aware and request-only critique for an artefact.

    Keyed by call-type value so callers can render them side by side.
    """
    inbound = await db.get_links_to(artefact_id)
    critique_link_ids = [l.from_page_id for l in inbound if l.link_type == LinkType.CRITIQUE_OF]
    if not critique_link_ids:
        return {}
    pages_by_id = await db.get_pages_by_ids(critique_link_ids)
    by_kind: dict[str, Page] = {}
    for p in pages_by_id.values():
        if not p.is_active() or p.page_type != PageType.JUDGEMENT:
            continue
        kind = p.provenance_call_type or ""
        existing = by_kind.get(kind)
        if existing is None or p.created_at > existing.created_at:
            by_kind[kind] = p
    return by_kind


MOVE = MoveDef(
    move_type=MoveType.REGENERATE_AND_CRITIQUE,
    name="regenerate_and_critique",
    description=(
        "Regenerate the artefact from the current spec and produce two fresh, "
        "independent critiques of it: one with workspace context and one based "
        "purely on the request text. Fires all three sub-calls atomically so "
        "the critiques you see always match the latest artefact. Use after a "
        "batch of spec edits when you want to see how the artefact has moved. "
        "Each invocation consumes 3 units of budget (artefact + two critiques)."
    ),
    schema=RegenerateAndCritiquePayload,
    execute=execute,
)
