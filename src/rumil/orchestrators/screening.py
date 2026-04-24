"""Candidate screening for main-phase prioritization when a view exists.

When a view exists on a question, main-phase prioritization runs a screening
pass over a heterogeneous candidate pool — view items, their cited
claims/subquestions, direct subquestions and considerations of the scope
question, and each scout call type — and narrows the pool to a shortlist
before per-item scoring. See the `two_phase_main_phase_prioritization`
orchestration flow.
"""

import asyncio
import logging
from collections.abc import Sequence
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from rumil.calls.dispatches import DISPATCH_DEFS
from rumil.database import DB
from rumil.llm import LLMExchangeMetadata, build_system_prompt, structured_call
from rumil.models import CallType, LinkType, Page, PageType
from rumil.tracing.trace_events import (
    CandidatesBuiltEvent,
    CandidatesScreenedEvent,
    CandidateTraceItem,
    ScreenDecisionTraceItem,
)
from rumil.tracing.tracer import CallTrace

log = logging.getLogger(__name__)

# Outgoing link kinds on a view item that count as "this item rests on / points at
# something investigable" for candidate expansion.
_VIEW_ITEM_CITATION_LINKS: set[LinkType] = {
    LinkType.DEPENDS_ON,
    LinkType.CITES,
    LinkType.RELATED,
}

# Page types that are meaningful to screen as candidates. Sources, wikis,
# summaries and judgements are reachable via view item citations but aren't
# investigation targets themselves.
_SCREENABLE_PAGE_TYPES: set[PageType] = {PageType.CLAIM, PageType.QUESTION}


class ScreenCandidateKind(str, Enum):
    PAGE = "page"
    SCOUT = "scout"


class ScreenCandidate(BaseModel):
    """One item put in front of the screening LLM call.

    Either a page (view_item, claim, subquestion) identified by page id, or a
    scout call type identified by its `CallType` value string.
    """

    kind: ScreenCandidateKind
    ref: str = Field(
        description=(
            "Page id (kind=page) or CallType value such as 'scout_hypotheses' (kind=scout)."
        ),
    )
    label: str = Field(description="Short human-readable label shown to the screening LLM.")
    signals: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Kind-specific priors. view_item pages: robustness, credence, importance, "
            "section. subquestion pages: has_own_view, child_count. claim pages: (none "
            "required). scouts: last_fruit (0-10 or null)."
        ),
    )
    provenance: list[str] = Field(
        default_factory=list,
        description=(
            "Why this candidate is in the pool. Example entries: 'direct_subquestion', "
            "'direct_consideration', 'view_item:<view_id>', 'cited_by:<view_item_id>', "
            "'scout_type'."
        ),
    )


class ScreenDecision(BaseModel):
    """One per-candidate decision from the screening LLM call.

    `suggested_call_type` is a raw string (not CallType) to tolerate LLM output
    that doesn't match any known call type — we validate and drop in the
    consumer rather than failing the whole structured parse.
    """

    ref: str = Field(description="Must match a ScreenCandidate.ref.")
    investigate: bool = Field(description="True if this candidate is worth scoring.")
    suggested_call_type: str | None = Field(
        default=None,
        description=(
            "Suggested dispatch type when investigate=True. Page candidates: typically "
            "'assess', 'find_considerations', 'web_research', or a 'scout_*' value. "
            "Scout candidates: must equal the scout's own call_type value. "
            "Ignored when investigate=False."
        ),
    )
    reason: str = Field(description="One-line justification.")


class ScreenResult(BaseModel):
    """Top-level response wrapper for the screening structured call."""

    decisions: list[ScreenDecision]


class ScoutFruitScore(BaseModel):
    """One per-scout fruit assessment from the scout-scoring LLM call."""

    call_type: str = Field(description="Scout call_type, e.g. 'scout_hypotheses'.")
    fruit: int = Field(ge=0, le=10, description="0-10 remaining fruit.")
    reasoning: str = Field(description="One-sentence explanation.")


class ScoutFruitScoringResult(BaseModel):
    scores: list[ScoutFruitScore]


class ScreenedCandidate(BaseModel):
    """A candidate that survived screening, bundled with its decision.

    The screening helper returns these so downstream scoring/dispatch doesn't
    need to re-join candidates to decisions by `ref`.
    """

    candidate: ScreenCandidate
    suggested_call_type: CallType | None
    reason: str


def merge_provenance(
    existing: Sequence[str],
    incoming: Sequence[str],
) -> list[str]:
    """Order-preserving union of provenance tags."""
    seen: set[str] = set()
    out: list[str] = []
    for tag in list(existing) + list(incoming):
        if tag not in seen:
            seen.add(tag)
            out.append(tag)
    return out


def scout_types_from(dispatch_types: Sequence[CallType]) -> list[CallType]:
    """Filter a dispatch-type list down to scout call types."""
    return [ct for ct in dispatch_types if ct.value.startswith("scout_")]


async def candidates_from_view(
    question_id: str,
    db: DB,
) -> list[ScreenCandidate]:
    """Build view-item candidates and their cited claim/subquestion candidates.

    Returns an empty list if no view exists for the question.
    """
    view = await db.get_view_for_question(question_id)
    if view is None:
        return []
    items = await db.get_view_items(view.id)
    if not items:
        return []

    candidates: list[ScreenCandidate] = []
    for page, link in items:
        candidates.append(
            ScreenCandidate(
                kind=ScreenCandidateKind.PAGE,
                ref=page.id,
                label=page.headline,
                signals={
                    "page_type": page.page_type.value,
                    "robustness": page.robustness,
                    "importance": link.importance,
                    "section": link.section,
                },
                provenance=[f"view_item:{view.id}"],
            )
        )

    item_ids = [p.id for p, _ in items]
    outgoing_by_item = await db.get_links_from_many(item_ids)

    cited_by: dict[str, list[str]] = {}
    for item_id, links in outgoing_by_item.items():
        for link in links:
            if link.link_type in _VIEW_ITEM_CITATION_LINKS:
                cited_by.setdefault(link.to_page_id, []).append(item_id)

    if not cited_by:
        return candidates

    cited_pages = await db.get_pages_by_ids(list(cited_by.keys()))
    for pid, page in cited_pages.items():
        if not page.is_active():
            continue
        if page.page_type not in _SCREENABLE_PAGE_TYPES:
            continue
        candidates.append(
            ScreenCandidate(
                kind=ScreenCandidateKind.PAGE,
                ref=pid,
                label=page.headline,
                signals={
                    "page_type": page.page_type.value,
                    "robustness": page.robustness,
                    "credence": page.credence,
                },
                provenance=[f"cited_by:{vi}" for vi in cited_by[pid]],
            )
        )
    return candidates


async def candidates_from_scope_question(
    question_id: str,
    db: DB,
) -> list[ScreenCandidate]:
    """Build direct subquestion and consideration candidates on the scope question."""
    child_questions, considerations = await asyncio.gather(
        db.get_child_questions(question_id),
        db.get_considerations_for_question(question_id),
    )

    subq_ids = [q.id for q in child_questions]
    views_by_subq = (
        await db.get_views_for_questions(subq_ids) if subq_ids else {}
    )

    candidates: list[ScreenCandidate] = []
    for q in child_questions:
        candidates.append(
            ScreenCandidate(
                kind=ScreenCandidateKind.PAGE,
                ref=q.id,
                label=q.headline,
                signals={
                    "page_type": PageType.QUESTION.value,
                    "has_own_view": views_by_subq.get(q.id) is not None,
                },
                provenance=["direct_subquestion"],
            )
        )
    for page, _link in considerations:
        candidates.append(
            ScreenCandidate(
                kind=ScreenCandidateKind.PAGE,
                ref=page.id,
                label=page.headline,
                signals={
                    "page_type": PageType.CLAIM.value,
                    "robustness": page.robustness,
                    "credence": page.credence,
                },
                provenance=["direct_consideration"],
            )
        )
    return candidates


async def candidates_from_scouts(
    question_id: str,
    db: DB,
    scout_types: Sequence[CallType],
) -> list[ScreenCandidate]:
    """Build one candidate per scout call type, with the latest remaining fruit."""
    fruit_by_type = await db.get_latest_scout_fruit(question_id)
    return [
        ScreenCandidate(
            kind=ScreenCandidateKind.SCOUT,
            ref=ct.value,
            label=f"Run {ct.value}",
            signals={"last_fruit": fruit_by_type.get(ct.value)},
            provenance=["scout_type"],
        )
        for ct in scout_types
    ]


def _render_parent_block(
    parent_page: Page,
    parent_judgement: Page | None,
) -> str:
    parts = ["## Scope question", f"Headline: {parent_page.headline}"]
    if parent_page.abstract:
        parts.append("")
        parts.append(f"Abstract: {parent_page.abstract}")
    if parent_judgement:
        parts.append("")
        parts.append(
            f"Latest judgement (robustness {parent_judgement.robustness}/5):"
        )
        parts.append(parent_judgement.abstract or parent_judgement.headline)
    return "\n".join(parts)


def _render_candidates_block(candidates: Sequence[ScreenCandidate]) -> str:
    lines = [f"## Candidates ({len(candidates)})", ""]
    for i, c in enumerate(candidates, start=1):
        sig_parts = [
            f"{k}={v}" for k, v in c.signals.items() if v is not None
        ]
        sig_str = " · ".join(sig_parts)
        header = f"### {i}. [{c.kind.value}]"
        if sig_str:
            header = f"{header} {sig_str}"
        lines.append(header)
        lines.append(f"ref: `{c.ref}`")
        lines.append(f"label: {c.label}")
        if c.provenance:
            lines.append(f"provenance: {', '.join(c.provenance)}")
        lines.append("")
    return "\n".join(lines)


async def score_scouts(
    scout_types: Sequence[CallType],
    db: DB,
    *,
    call_id: str,
    parent_page: Page,
    parent_judgement: Page | None,
    view_render: str | None,
    last_fruit_by_type: dict[str, int | None],
) -> dict[str, dict[str, Any]]:
    """Ask the LLM to score remaining fruit for each scout type.

    Returns a mapping ``{call_type_value: {"fruit": int, "reasoning": str}}``.
    Scouts absent from the response are simply omitted from the result — the
    caller decides what to do with gaps.
    """
    if not scout_types:
        return {}

    parent_block = _render_parent_block(parent_page, parent_judgement)
    scout_blocks: list[str] = []
    for ct in scout_types:
        ddef = DISPATCH_DEFS.get(ct)
        description = ddef.description if ddef else ""
        last = last_fruit_by_type.get(ct.value)
        last_line = (
            f"last_fruit: {last}/10" if last is not None else "last_fruit: null (never run)"
        )
        scout_blocks.append(
            f"### `{ct.value}`\n{description}\n{last_line}"
        )
    scouts_block = "## Scout types\n\n" + "\n\n".join(scout_blocks)

    parts = [parent_block]
    if view_render:
        parts.append(view_render)
    parts.append(scouts_block)
    parts.append("Score each scout now — one entry per scout in `scores`.")
    user_message = "\n\n".join(parts)

    system_prompt = build_system_prompt("score_scouts", include_citations=False)

    result = await structured_call(
        system_prompt,
        user_message=user_message,
        response_model=ScoutFruitScoringResult,
        cache=True,
        metadata=LLMExchangeMetadata(
            call_id=call_id,
            phase="score_scouts",
            user_messages=[{"role": "user", "content": user_message}],
        ),
        db=db,
    )

    out: dict[str, dict[str, Any]] = {}
    if result.parsed:
        for s in result.parsed.scores:
            out[s.call_type] = {"fruit": s.fruit, "reasoning": s.reasoning}
    return out


async def screen_candidates(
    question_id: str,
    db: DB,
    scout_types: Sequence[CallType],
    *,
    call_id: str,
    trace: CallTrace,
    parent_page: Page,
    parent_judgement: Page | None,
    view_render: str | None = None,
) -> list[ScreenedCandidate]:
    """Run the screening pass: build pool, emit trace events, return survivors.

    Callers should use the returned list as the candidate set for subsequent
    scoring + dispatch. An empty return means either no candidates existed or
    the screen rejected them all; in both cases, main-phase should bail out of
    this iteration.
    """
    candidates = await build_candidate_pool(question_id, db, scout_types)

    await trace.record(
        CandidatesBuiltEvent(
            candidates=[
                CandidateTraceItem(
                    kind=c.kind.value,
                    ref=c.ref,
                    label=c.label,
                    signals=c.signals,
                    provenance=list(c.provenance),
                )
                for c in candidates
            ],
        )
    )

    if not candidates:
        await trace.record(CandidatesScreenedEvent(decisions=[]))
        return []

    parent_block = _render_parent_block(parent_page, parent_judgement)
    candidates_block = _render_candidates_block(candidates)
    blocks = [parent_block]
    if view_render:
        blocks.append(view_render)
    blocks.append(candidates_block)
    blocks.append(
        "Now output your screening decisions as a `decisions` list. "
        "One entry per candidate, `ref` matching the candidate exactly."
    )
    user_message = "\n\n".join(blocks)

    system_prompt = build_system_prompt("screen_candidates", include_citations=False)

    result = await structured_call(
        system_prompt,
        user_message=user_message,
        response_model=ScreenResult,
        cache=True,
        metadata=LLMExchangeMetadata(
            call_id=call_id,
            phase="screen_candidates",
            user_messages=[{"role": "user", "content": user_message}],
        ),
        db=db,
    )

    decisions: list[ScreenDecision] = list(result.parsed.decisions) if result.parsed else []

    await trace.record(
        CandidatesScreenedEvent(
            decisions=[
                ScreenDecisionTraceItem(
                    ref=d.ref,
                    investigate=d.investigate,
                    suggested_call_type=d.suggested_call_type,
                    reason=d.reason,
                )
                for d in decisions
            ],
        )
    )

    decisions_by_ref: dict[str, ScreenDecision] = {d.ref: d for d in decisions}
    survivors: list[ScreenedCandidate] = []
    for c in candidates:
        d = decisions_by_ref.get(c.ref)
        if d is None or not d.investigate:
            continue
        ct: CallType | None = None
        if d.suggested_call_type:
            try:
                ct = CallType(d.suggested_call_type)
            except ValueError:
                log.warning(
                    "Screen decision for %s suggested unknown call_type %r; dropping",
                    c.ref[:8] if c.kind == ScreenCandidateKind.PAGE else c.ref,
                    d.suggested_call_type,
                )
                continue
        if ct is None:
            log.warning(
                "Screen kept %s with no suggested_call_type; dropping",
                c.ref[:8] if c.kind == ScreenCandidateKind.PAGE else c.ref,
            )
            continue
        survivors.append(
            ScreenedCandidate(
                candidate=c,
                suggested_call_type=ct,
                reason=d.reason,
            )
        )
    return survivors


async def build_candidate_pool(
    question_id: str,
    db: DB,
    scout_types: Sequence[CallType],
) -> list[ScreenCandidate]:
    """Gather candidates from all three sources, deduping by ref.

    When a page appears via multiple paths (e.g. a claim that is both a direct
    consideration and cited by a view item), provenance tags and signals are
    merged — later-source values only overwrite earlier ones on key collision.
    """
    view_candidates, scope_candidates, scout_candidates = await asyncio.gather(
        candidates_from_view(question_id, db),
        candidates_from_scope_question(question_id, db),
        candidates_from_scouts(question_id, db, scout_types),
    )

    by_ref: dict[str, ScreenCandidate] = {}
    ordered_sources = list(view_candidates) + list(scope_candidates) + list(scout_candidates)
    for c in ordered_sources:
        existing = by_ref.get(c.ref)
        if existing is None:
            by_ref[c.ref] = c
            continue
        merged_signals = {**existing.signals, **{k: v for k, v in c.signals.items() if v is not None}}
        by_ref[c.ref] = existing.model_copy(
            update={
                "provenance": merge_provenance(existing.provenance, c.provenance),
                "signals": merged_signals,
            }
        )
    return list(by_ref.values())
