"""PROPOSE_HYPOTHESIS move: create a hypothesis claim and investigation question."""

import logging

from pydantic import BaseModel, Field

from differential.database import DB
from differential.models import (
    Call,
    ConsiderationDirection,
    LinkType,
    MoveType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)
from differential.moves.base import MoveDef, MoveResult, write_page_file

log = logging.getLogger(__name__)


class ProposeHypothesisPayload(BaseModel):
    parent_question_id: str = Field(description="Full UUID of the parent question")
    hypothesis: str = Field(
        description="Specific assertive statement of the hypothesis (not a question)"
    )
    reasoning: str = Field("", description="Why this hypothesis is worth investigating")
    epistemic_status: float = Field(2.5, description="0-5 subjective confidence")
    direction: str = Field("neutral", description="supports, opposes, or neutral")
    strength: float = Field(2.5, description="0-5 consideration strength")


def execute(payload: ProposeHypothesisPayload, call: Call, db: DB) -> MoveResult:
    parent_id = db.resolve_page_id(payload.parent_question_id)
    if not parent_id:
        log.warning(
            "PROPOSE_HYPOTHESIS: parent_question_id not found: %s",
            payload.parent_question_id,
        )
        return MoveResult("Hypothesis skipped — parent question not found.")

    if not payload.hypothesis.strip():
        log.warning("PROPOSE_HYPOTHESIS: missing hypothesis text")
        return MoveResult("Hypothesis skipped — missing hypothesis text.")

    # 1. Create the claim
    claim_content = payload.hypothesis
    if payload.reasoning:
        claim_content += f"\n\n{payload.reasoning}"

    claim = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=claim_content,
        summary=payload.hypothesis[:120],
        epistemic_status=payload.epistemic_status,
        epistemic_type="hypothesis",
        provenance_model="claude-opus-4-6",
        provenance_call_type=call.call_type.value,
        provenance_call_id=call.id,
        extra={"hypothesis": True},
    )
    db.save_page(claim)
    write_page_file(claim)
    log.info("Hypothesis claim created: %s", db.page_label(claim.id))

    direction_str = payload.direction.lower()
    try:
        direction = ConsiderationDirection(direction_str)
    except ValueError:
        log.debug("Invalid direction '%s' defaulting to neutral", direction_str)
        direction = ConsiderationDirection.NEUTRAL

    db.save_link(
        PageLink(
            from_page_id=claim.id,
            to_page_id=parent_id,
            link_type=LinkType.CONSIDERATION,
            direction=direction,
            strength=payload.strength,
            reasoning=payload.reasoning,
        )
    )
    log.info(
        "Consideration linked: %s -> %s (%s)",
        claim.id[:8], parent_id[:8], direction_str,
    )

    # 2. Create the hypothesis question
    q_text = f"What should we make of the hypothesis that {payload.hypothesis}?"
    question = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=q_text,
        summary=q_text[:120],
        epistemic_status=2.5,
        epistemic_type="open question",
        provenance_model="claude-opus-4-6",
        provenance_call_type=call.call_type.value,
        provenance_call_id=call.id,
        extra={"hypothesis": True, "status": "open"},
    )
    db.save_page(question)
    write_page_file(question)
    log.info("Hypothesis question created: %s", db.page_label(question.id))

    db.save_link(
        PageLink(
            from_page_id=parent_id,
            to_page_id=question.id,
            link_type=LinkType.CHILD_QUESTION,
            reasoning=f"Hypothesis: {payload.hypothesis[:80]}",
        )
    )
    log.info(
        "Child question linked: %s -> %s",
        parent_id[:8], question.id[:8],
    )

    return MoveResult(
        f"Created hypothesis claim [{claim.id[:8]}] and question [{question.id[:8]}].",
        created_page_id=question.id,
    )


MOVE = MoveDef(
    move_type=MoveType.PROPOSE_HYPOTHESIS,
    name="propose_hypothesis",
    description=(
        "Propose a hypothesis for investigation. This creates a claim linked "
        "to the parent question as a consideration AND a hypothesis question "
        "'What should we make of the hypothesis that...?' for focused "
        "investigation. Use when you have a compelling candidate answer, not "
        "just a piece of evidence."
    ),
    schema=ProposeHypothesisPayload,
    execute=execute,
)
