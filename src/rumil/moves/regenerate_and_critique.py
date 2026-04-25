"""REGENERATE_AND_CRITIQUE: fire generate_artefact + critique_artefact in sequence.

Called from the refine_spec agent loop. Regenerating without a fresh critique,
or critiquing without a fresh artefact, would desynchronise the signal the
refiner reads — so the two sub-calls are bundled into a single atomic tool.
"""

import logging

from pydantic import BaseModel, Field

from rumil.database import DB
from rumil.models import Call, CallType, LinkType, MoveType, PageType
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
    from rumil.calls.generate_artefact import GenerateArtefactCall

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

    if not await db.consume_budget(2):
        return MoveResult(
            message=(
                "ERROR: regenerate_and_critique needs 2 units of budget and "
                "fewer are available. No sub-calls fired. Consider calling "
                "finalize_artefact to ship the latest artefact instead."
            ),
            created_page_id=None,
        )

    gen_call = await db.create_call(
        CallType.GENERATE_ARTEFACT,
        scope_page_id=artefact_task_id,
        parent_call_id=call.id,
    )
    gen_runner = GenerateArtefactCall(artefact_task_id, gen_call, db)
    await gen_runner.run()

    crit_call = await db.create_call(
        CallType.CRITIQUE_ARTEFACT,
        scope_page_id=artefact_task_id,
        parent_call_id=call.id,
    )
    crit_runner = CritiqueArtefactCall(artefact_task_id, crit_call, db)
    await crit_runner.run()

    new_artefact = await db.latest_artefact_for_task(artefact_task_id)
    if new_artefact is None:
        return MoveResult(
            message="ERROR: after regenerate_and_critique, no artefact exists for the task.",
            created_page_id=None,
        )

    critique_links = await db.get_links_to(new_artefact.id)
    critique_pages_by_id: dict = {}
    critique_link_ids = [
        l.from_page_id for l in critique_links if l.link_type == LinkType.CRITIQUE_OF
    ]
    if critique_link_ids:
        critique_pages_by_id = await db.get_pages_by_ids(critique_link_ids)
    critiques = [
        p
        for p in critique_pages_by_id.values()
        if p.is_active() and p.page_type == PageType.JUDGEMENT
    ]
    latest_critique = max(critiques, key=lambda p: p.created_at) if critiques else None

    grade = latest_critique.extra.get("grade") if latest_critique else None
    issues = latest_critique.extra.get("issues") if latest_critique else []
    issue_lines = "\n".join(f"- {issue}" for issue in (issues or []))

    summary_parts = [
        f"Regenerated artefact [{new_artefact.id[:8]}]: {new_artefact.headline}",
    ]
    if grade is not None:
        summary_parts.append(f"Critique grade: {grade}/10")
    if issue_lines:
        summary_parts.append("Issues surfaced:")
        summary_parts.append(issue_lines)
    summary = "\n\n".join(summary_parts)

    log.info(
        "regenerate_and_critique complete: artefact=%s, grade=%s, issue_count=%d",
        new_artefact.id[:8],
        grade,
        len(issues or []),
    )
    return MoveResult(
        message=summary,
        created_page_id=None,
        trace_extra={
            "artefact_id": new_artefact.id,
            "critique_id": latest_critique.id if latest_critique else None,
            "grade": grade,
        },
    )


MOVE = MoveDef(
    move_type=MoveType.REGENERATE_AND_CRITIQUE,
    name="regenerate_and_critique",
    description=(
        "Regenerate the artefact from the current spec and produce a fresh, "
        "independent critique of it. Fires both sub-calls atomically so the "
        "critique you see always matches the latest artefact. Use after a "
        "batch of spec edits when you want to see how the artefact has moved. "
        "Each invocation consumes 2 units of budget (one per sub-call)."
    ),
    schema=RegenerateAndCritiquePayload,
    execute=execute,
)
