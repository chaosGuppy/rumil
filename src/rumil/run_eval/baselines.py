"""Single-call baseline: what would a strong model produce in one shot?

The orchestrator-vs-baseline comparison is Path A common-primitive work —
without this baseline we can't tell whether a multi-call orchestrator run is
actually net-positive over just asking the biggest model with the same context.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from rumil.context import format_page
from rumil.database import DB
from rumil.llm import LLMExchangeMetadata, structured_call
from rumil.models import CallType, Page, PageDetail, PageType
from rumil.pricing import compute_cost
from rumil.settings import get_settings
from rumil.tracing.tracer import CallTrace

log = logging.getLogger(__name__)

_BASELINE_SYSTEM_PROMPT = (
    "You are an experienced research reviewer producing a distilled view of a "
    "research workspace in a single pass. You will be given a root question "
    "plus the considerations and judgements that have been gathered. Produce a "
    "compact view: headline findings, credence-weighted claims (credence on the "
    "1-9 scale where 5 = genuinely uncertain, 9 = nearly certain), and the key "
    "remaining uncertainties. Be calibrated. Do not invent facts not present in "
    "the provided context."
)


class HeadlineClaim(BaseModel):
    """One credence-weighted claim in the baseline's distilled view."""

    claim: str = Field(description="A single, falsifiable statement.")
    credence: int = Field(
        ge=1,
        le=9,
        description="Credence on 1-9 scale (5 = genuinely uncertain, 9 = near-certain).",
    )
    reasoning: str = Field(default="", description="Why this credence is justified.")


class Uncertainty(BaseModel):
    """A key uncertainty that would move the baseline's view if resolved."""

    description: str
    why_it_matters: str = ""


class BaselineView(BaseModel):
    """Structured single-call baseline output."""

    headline: str = Field(description="One-sentence top-level answer.")
    summary: str = Field(default="", description="Paragraph-level synthesis.")
    claims: list[HeadlineClaim] = Field(default_factory=list)
    uncertainties: list[Uncertainty] = Field(default_factory=list)


@dataclass
class SingleCallBaselineResult:
    """Result of a single-call baseline against a question."""

    question_id: str
    model: str
    response_text: str
    view: BaselineView | None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    call_id: str | None = None
    context_text: str = ""
    context_page_ids: list[str] = field(default_factory=list)


async def _gather_baseline_context(
    question_id: str,
    db: DB,
    *,
    max_claims: int,
) -> tuple[str, list[str]]:
    """Render the question + its considerations/judgements as LLM context.

    Uses the same `format_page` machinery the orchestrator would use when
    distilling, so the baseline gets a fair comparison: same raw material,
    single shot vs multi-call.
    """
    question = await db.get_page(question_id)
    if question is None:
        raise ValueError(f"Question {question_id} not found")
    if question.page_type != PageType.QUESTION:
        raise ValueError(f"Page {question_id} is not a question (type={question.page_type.value})")

    parts: list[str] = []
    loaded_ids: list[str] = [question.id]
    parts.append("## Root Question\n")
    parts.append(
        await format_page(
            question,
            PageDetail.CONTENT,
            linked_detail=None,
            db=db,
        )
    )

    considerations = await db.get_considerations_for_question(question_id)
    if considerations:
        parts.append("\n## Considerations\n")
        for claim_page, _link in considerations[:max_claims]:
            loaded_ids.append(claim_page.id)
            parts.append(
                await format_page(
                    claim_page,
                    PageDetail.CONTENT,
                    linked_detail=None,
                    db=db,
                )
            )
            parts.append("")

    child_questions = await db.get_child_questions(question_id)
    if child_questions:
        parts.append("\n## Sub-questions\n")
        for child in child_questions:
            loaded_ids.append(child.id)
            parts.append(
                await format_page(
                    child,
                    PageDetail.ABSTRACT,
                    linked_detail=None,
                    db=db,
                )
            )

    context_text = "\n".join(parts)
    return context_text, loaded_ids


async def run_single_call_baseline(
    db: DB,
    question_id: str,
    model: str = "claude-opus-4-7",
    budget_ceiling_tokens: int = 100_000,
    *,
    broadcaster=None,
) -> SingleCallBaselineResult:
    """Fire a single-shot baseline against the question.

    Loads the question plus its considerations and sub-questions using the same
    context machinery the orchestrator uses when distilling, then asks the
    specified model to produce a structured `BaselineView` in one call.

    Writes a `Call` row with `call_type=SINGLE_CALL_BASELINE` (non-dispatchable)
    so the baseline is visible alongside orchestrator calls in traces.

    *budget_ceiling_tokens* is a soft ceiling — we truncate the rendered context
    to roughly this many characters (4 chars/token approximation) to avoid
    blowing the context window.
    """
    settings = get_settings()
    max_claims = settings.single_call_baseline_max_claims

    context_text, context_page_ids = await _gather_baseline_context(
        question_id,
        db,
        max_claims=max_claims,
    )

    char_ceiling = budget_ceiling_tokens * 4
    if len(context_text) > char_ceiling:
        log.info(
            "Single-call baseline context truncated: %d -> %d chars",
            len(context_text),
            char_ceiling,
        )
        context_text = context_text[:char_ceiling] + "\n\n[... context truncated ...]"

    call = await db.create_call(
        call_type=CallType.SINGLE_CALL_BASELINE,
        scope_page_id=question_id,
        context_page_ids=context_page_ids,
    )
    trace = CallTrace(call.id, db, broadcaster=broadcaster)

    user_message = (
        f"{context_text}\n\n---\n\n"
        "Given this research, produce a distilled view. Include headline "
        "findings, credence-weighted claims, and the key remaining "
        "uncertainties."
    )

    try:
        result = await structured_call(
            system_prompt=_BASELINE_SYSTEM_PROMPT,
            user_message=user_message,
            response_model=BaselineView,
            metadata=LLMExchangeMetadata(
                call_id=call.id,
                phase="single_call_baseline",
            ),
            db=db,
            model=model,
        )
    except Exception:
        log.exception("Single-call baseline failed for question %s", question_id)
        raise

    input_tokens = result.input_tokens or 0
    output_tokens = result.output_tokens or 0
    cost_usd = compute_cost(
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )

    if trace.total_cost_usd > 0:
        call.cost_usd = trace.total_cost_usd
    elif cost_usd > 0:
        call.cost_usd = cost_usd
    call.result_summary = (result.response_text or "")[:500]
    await db.save_call(call)

    return SingleCallBaselineResult(
        question_id=question_id,
        model=model,
        response_text=result.response_text or "",
        view=result.parsed,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        call_id=call.id,
        context_text=context_text,
        context_page_ids=list(context_page_ids),
    )


def render_baseline_view(view: BaselineView | None, response_text: str) -> str:
    """Render a baseline view as Markdown for inclusion in eval reports.

    Falls back to the raw response text when structured parsing failed.
    """
    if view is None:
        return response_text or "(baseline produced no output)"

    parts: list[str] = [f"# {view.headline}"]
    if view.summary:
        parts.append("\n" + view.summary)
    if view.claims:
        parts.append("\n## Claims\n")
        for claim in view.claims:
            parts.append(f"- **C{claim.credence}** — {claim.claim}")
            if claim.reasoning:
                parts.append(f"  - _Reasoning:_ {claim.reasoning}")
    if view.uncertainties:
        parts.append("\n## Key Uncertainties\n")
        for unc in view.uncertainties:
            parts.append(f"- {unc.description}")
            if unc.why_it_matters:
                parts.append(f"  - _Why:_ {unc.why_it_matters}")
    return "\n".join(parts)


def summarize_context_pages(pages: Sequence[Page]) -> str:
    """Human-readable summary of which pages were fed to the baseline."""
    if not pages:
        return "(none)"
    return ", ".join(f"`{p.id[:8]}` {p.headline}" for p in pages)
