"""Tension detection primitive.

A *tension* is structurally: two (or more) claims bearing on the same
question, at credence >= 6, with directions that conflict (one SUPPORTS
and one OPPOSES), OR with explicit content-level contradiction that an
LLM flags.

This module exposes ``find_tension_candidates`` — a workspace-read-only
primitive that surfaces candidate tensions on a question. It does NOT
run the expensive explorer call itself; downstream callers
(``TensionExplorationPolicy``, CLI tooling) are responsible for deciding
which candidates are worth investigating.

Two detection strategies are available:

- **direction_conflict** (cheap, structural): pairs of high-credence claims
  whose ``ConsiderationDirection`` values disagree on the same question.
- **semantic_contradiction** (LLM-gated): pairs the structural scan did
  NOT flag, passed through a small LLM prompt that returns a structured
  yes/no + reason + confidence.

Callers can request both or just the cheap pass — see ``find_tension_candidates``
for the ``include_semantic`` flag. The semantic pass is opt-in because it
burns budget; the MVP integration uses the cheap pass only.
"""

from __future__ import annotations

import itertools
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from rumil.database import DB
from rumil.llm import LLMExchangeMetadata, _load_file, structured_call
from rumil.models import ConsiderationDirection, Page, PageLink, SuggestionType

log = logging.getLogger(__name__)


TENSION_DETECTOR_PROMPT_FILE = "tension_detector.md"

TENSION_CREDENCE_THRESHOLD = 6

TensionKind = Literal[
    "direction_conflict",
    "semantic_contradiction",
]


@dataclass(frozen=True)
class TensionCandidate:
    """A candidate tension between two claims on a shared question.

    ``claim_a_id`` and ``claim_b_id`` are ordered lexicographically so that
    a given unordered pair has a canonical representation — this lets
    downstream code deduplicate candidates across detection passes and check
    "is this tension already explored?" without caring about argument order.
    """

    question_id: str
    claim_a_id: str
    claim_b_id: str
    kind: TensionKind
    reason: str
    confidence: float

    @classmethod
    def make(
        cls,
        question_id: str,
        claim_x_id: str,
        claim_y_id: str,
        *,
        kind: TensionKind,
        reason: str,
        confidence: float,
    ) -> TensionCandidate:
        """Construct a candidate with canonical claim-id ordering."""
        a, b = sorted([claim_x_id, claim_y_id])
        return cls(
            question_id=question_id,
            claim_a_id=a,
            claim_b_id=b,
            kind=kind,
            reason=reason,
            confidence=confidence,
        )

    @property
    def pair_key(self) -> tuple[str, str, str]:
        """Stable key for deduplication across detection passes / calls."""
        return (self.question_id, self.claim_a_id, self.claim_b_id)


class _TensionVerdict(BaseModel):
    """Structured output for the semantic-contradiction LLM prompt."""

    in_tension: bool = Field(description="True if the two claims are in genuine tension.")
    reason: str = Field(description="1-2 sentence explanation of the friction (or lack thereof).")
    confidence: float = Field(ge=0.0, le=1.0, description="Model's confidence in the call.")
    kind: Literal[
        "semantic_contradiction",
        "scope_conflict",
        "degree_conflict",
        "none",
    ] = Field(description="Nature of the tension, or 'none' if in_tension is false.")


def _conflicting_directions(
    a: ConsiderationDirection | None, b: ConsiderationDirection | None
) -> bool:
    """Two direction values conflict if one SUPPORTS and the other OPPOSES."""
    if a is None or b is None:
        return False
    return {a, b} == {ConsiderationDirection.SUPPORTS, ConsiderationDirection.OPPOSES}


def _direction_conflict_candidates(
    question_id: str,
    considerations: Sequence[tuple[Page, PageLink]],
    *,
    credence_threshold: int,
) -> list[TensionCandidate]:
    """Cheap structural scan for direction-conflict tensions.

    Considers only active claims at credence >= threshold. Emits one
    candidate per unordered (claim_a, claim_b) pair where directions
    conflict on the shared question.
    """
    eligible = [
        (page, link)
        for page, link in considerations
        if page.credence is not None and page.credence >= credence_threshold
    ]
    candidates: list[TensionCandidate] = []
    for (page_a, link_a), (page_b, link_b) in itertools.combinations(eligible, 2):
        if not _conflicting_directions(link_a.direction, link_b.direction):
            continue
        reason = (
            f"Both claims sit at credence >= {credence_threshold}, but "
            f"'{page_a.headline[:60]}' {link_a.direction.value if link_a.direction else '?'} "
            f"the question while '{page_b.headline[:60]}' "
            f"{link_b.direction.value if link_b.direction else '?'} it."
        )
        candidates.append(
            TensionCandidate.make(
                question_id,
                page_a.id,
                page_b.id,
                kind="direction_conflict",
                reason=reason,
                confidence=1.0,
            )
        )
    return candidates


async def _semantic_contradiction_candidate(
    db: DB,
    question: Page,
    page_a: Page,
    page_b: Page,
    *,
    call_id: str | None = None,
) -> TensionCandidate | None:
    """Run the LLM tension-detector on a single claim pair.

    Returns a ``TensionCandidate`` iff the detector flags the pair as in
    tension; otherwise returns None. The prompt is system-prompt only; the
    user message assembles the question + both claims.
    """
    system_prompt = _load_file(TENSION_DETECTOR_PROMPT_FILE)
    user_message = (
        f"## Question\n\n{question.headline}\n\n{question.content}\n\n"
        f"## Claim A (`{page_a.id[:8]}`, credence={page_a.credence})\n\n"
        f"{page_a.headline}\n\n{page_a.content}\n\n"
        f"## Claim B (`{page_b.id[:8]}`, credence={page_b.credence})\n\n"
        f"{page_b.headline}\n\n{page_b.content}\n\n"
        "Produce your structured tension verdict now."
    )
    metadata = LLMExchangeMetadata(
        call_id=call_id or "",
        phase="tension_detection",
        user_message=user_message,
    )
    result = await structured_call(
        system_prompt=system_prompt,
        user_message=user_message,
        response_model=_TensionVerdict,
        metadata=metadata,
        db=db,
    )
    verdict = result.parsed
    if verdict is None or not verdict.in_tension:
        return None
    return TensionCandidate.make(
        question.id,
        page_a.id,
        page_b.id,
        kind="semantic_contradiction",
        reason=verdict.reason,
        confidence=verdict.confidence,
    )


async def find_tension_candidates(
    db: DB,
    question_id: str,
    *,
    credence_threshold: int = TENSION_CREDENCE_THRESHOLD,
    include_semantic: bool = False,
    max_semantic_pairs: int = 3,
    call_id: str | None = None,
) -> list[TensionCandidate]:
    """Return candidate tensions among considerations on *question_id*.

    Always runs the cheap structural (direction-conflict) scan. When
    ``include_semantic`` is True, the top ``max_semantic_pairs`` claim
    pairs that were NOT already flagged structurally are passed through
    the LLM tension detector (ranked by combined credence).

    Deduplicates across passes: if a pair is flagged both structurally and
    semantically, only the structural candidate is returned (it is
    cheaper and higher-signal).
    """
    question = await db.get_page(question_id)
    if question is None:
        log.warning("find_tension_candidates: question %s not found", question_id[:8])
        return []

    considerations = await db.get_considerations_for_question(question_id)
    if not considerations:
        return []

    structural = _direction_conflict_candidates(
        question_id,
        considerations,
        credence_threshold=credence_threshold,
    )
    if not include_semantic:
        return structural

    seen_pairs = {c.pair_key for c in structural}
    eligible = [
        (page, link)
        for page, link in considerations
        if page.credence is not None and page.credence >= credence_threshold
    ]
    eligible.sort(key=lambda pl: pl[0].credence or 0, reverse=True)

    semantic: list[TensionCandidate] = []
    probed = 0
    for (page_a, _), (page_b, _) in itertools.combinations(eligible, 2):
        if probed >= max_semantic_pairs:
            break
        a, b = sorted([page_a.id, page_b.id])
        if (question_id, a, b) in seen_pairs:
            continue
        probed += 1
        candidate = await _semantic_contradiction_candidate(
            db,
            question,
            page_a,
            page_b,
            call_id=call_id,
        )
        if candidate is not None:
            semantic.append(candidate)
            seen_pairs.add(candidate.pair_key)
    return [*structural, *semantic]


async def _already_explored_pair_keys(
    db: DB,
    question_id: str,
) -> set[tuple[str, str, str]]:
    """Return canonical pair keys that already have an EXPLORE_TENSION verdict.

    Looks for JUDGEMENT pages with ``extra.tension_pair`` matching the
    pattern written by ``ExploreTensionCall``. Also considers
    RESOLVE_TENSION suggestions as a softer "already surfaced" signal so
    we don't keep re-emitting the same suggestion on every iteration.
    """
    pair_keys: set[tuple[str, str, str]] = set()

    getter = getattr(db, "get_pending_suggestions", None)
    if getter is not None:
        try:
            suggestions = await getter()
        except Exception:
            log.debug("tension dedup: get_pending_suggestions failed", exc_info=True)
            suggestions = []
        for s in suggestions:
            if s.suggestion_type != SuggestionType.RESOLVE_TENSION:
                continue
            payload = s.payload or {}
            qid = payload.get("question_id")
            a = payload.get("claim_a_id")
            b = payload.get("claim_b_id")
            if qid and a and b:
                aa, bb = sorted([str(a), str(b)])
                pair_keys.add((str(qid), aa, bb))

    explored_getter = getattr(db, "get_tension_verdicts_for_question", None)
    if explored_getter is not None:
        try:
            verdicts = await explored_getter(question_id)
        except Exception:
            log.debug("tension dedup: get_tension_verdicts_for_question failed", exc_info=True)
            verdicts = []
        for page in verdicts:
            extra = page.extra or {}
            pair = extra.get("tension_pair") or {}
            qid = pair.get("question_id")
            a = pair.get("claim_a_id")
            b = pair.get("claim_b_id")
            if qid and a and b:
                aa, bb = sorted([str(a), str(b)])
                pair_keys.add((str(qid), aa, bb))
    return pair_keys


async def unexplored_tension_candidates(
    db: DB,
    question_id: str,
    *,
    credence_threshold: int = TENSION_CREDENCE_THRESHOLD,
    include_semantic: bool = False,
) -> list[TensionCandidate]:
    """Return candidates with no existing verdict / suggestion on the pair.

    Convenience wrapper around ``find_tension_candidates`` that filters out
    pairs already adjudicated by a previous ``ExploreTensionCall`` or
    already surfaced as a ``RESOLVE_TENSION`` suggestion.
    """
    candidates = await find_tension_candidates(
        db,
        question_id,
        credence_threshold=credence_threshold,
        include_semantic=include_semantic,
    )
    if not candidates:
        return []
    explored = await _already_explored_pair_keys(db, question_id)
    return [c for c in candidates if c.pair_key not in explored]
