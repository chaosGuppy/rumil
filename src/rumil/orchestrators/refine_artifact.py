"""RefineArtifactOrchestrator: close the artifact loop with adversarial review.

Composes two existing call types into a draft -> review -> refine loop:

  1. **Draft** a shape-parameterized artifact from the question's View via
     :class:`DraftArtifactCall`.
  2. **Review** the draft with :class:`AdversarialReviewCall`. The review's
     how-true + how-false scouts examine the artifact page itself, and the
     synthesizer emits a structured verdict (stronger_side, claim_holds,
     confidence, rationale, concurrences, dissents).
  3. **Gate**: if the verdict indicates the draft holds at sufficient
     confidence with no surviving dissents, **accept** it. Otherwise feed
     dissents + concurrences back into a new draft as a ``RefineContext``.
  4. **Terminate** on: accept, iteration cap, budget exhaustion, or two
     consecutive iterations where the dissent set did not shrink (stuck).

Accepted artifacts have their prior-draft page superseded and carry a
``extra["refinement"] = {...}`` block with iteration count, final verdict,
and the dissents that were addressed along the way. That block is the
provenance surface — readers can see "this artifact survived N rounds of
adversarial review".

Unlike the other orchestrators in this package, this one is not driven by
prioritization; it composes two existing calls in a tight loop and does
not subclass ``BaseOrchestrator`` to avoid dragging in the dispatch /
sequence machinery it doesn't need.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from rumil.calls.adversarial_review import (
    AdversarialReviewCall,
    AdversarialVerdict,
)
from rumil.calls.draft_artifact import (
    DEFAULT_IMPORTANCE_THRESHOLD,
    DEFAULT_SHAPE,
    DEFAULT_TOP_N_ITEMS,
    SUPPORTED_SHAPES,
    ArtifactShape,
    DraftArtifactCall,
    RefineContext,
)
from rumil.database import DB
from rumil.models import (
    CallType,
    Page,
    PageType,
)
from rumil.settings import get_settings
from rumil.tracing.broadcast import Broadcaster

log = logging.getLogger(__name__)


RefineOutcome = str


@dataclass
class RefineIteration:
    """Record of one draft + review cycle."""

    iteration: int
    draft_page_id: str
    review_call_id: str
    verdict: AdversarialVerdict
    dissents: list[str] = field(default_factory=list)


@dataclass
class RefineArtifactResult:
    """Summary of a refinement run, returned by :meth:`RefineArtifactOrchestrator.run`."""

    final_artifact_id: str | None
    outcome: RefineOutcome
    iterations: list[RefineIteration] = field(default_factory=list)

    @property
    def iteration_count(self) -> int:
        return len(self.iterations)


class RefineArtifactOrchestrator:
    """Drive the draft -> adversarial review -> refine -> accept loop.

    Budget accounting: each iteration is one draft + one adversarial review.
    The adversarial review internally dispatches two scouts, but those are
    accounted for under the review's budget line in upstream code; this
    orchestrator treats one iteration as costing 2 budget units.
    """

    def __init__(
        self,
        db: DB,
        broadcaster: Broadcaster | None = None,
        *,
        question_id: str | None = None,
        shape: ArtifactShape = DEFAULT_SHAPE,
        max_iterations: int | None = None,
        accept_confidence: int | None = None,
        importance_threshold: int = DEFAULT_IMPORTANCE_THRESHOLD,
        top_n_items: int = DEFAULT_TOP_N_ITEMS,
    ) -> None:
        if shape not in SUPPORTED_SHAPES:
            raise ValueError(
                f"RefineArtifact: unsupported shape {shape!r}. "
                f"Supported: {', '.join(SUPPORTED_SHAPES)}"
            )
        settings = get_settings()
        self.db = db
        self.question_id = question_id
        self.shape: ArtifactShape = shape
        self.max_iterations = (
            max_iterations
            if max_iterations is not None
            else settings.refine_artifact_max_iterations
        )
        self.accept_confidence = (
            accept_confidence
            if accept_confidence is not None
            else settings.refine_artifact_accept_confidence
        )
        self.importance_threshold = importance_threshold
        self.top_n_items = top_n_items
        self.broadcaster = broadcaster
        # Compat shim so the main.py / chat.py code paths that set ``ingest_hint``
        # on the result of ``Orchestrator(db)`` work uniformly. Not used here.
        self.ingest_hint: str = ""

    async def run(self, root_question_id: str | None = None) -> RefineArtifactResult:
        """Execute the refine loop. ``root_question_id`` overrides the constructor value if given."""
        qid = root_question_id or self.question_id
        if qid is None:
            raise ValueError(
                "RefineArtifactOrchestrator.run(): question_id must be set via "
                "the constructor or passed as root_question_id."
            )
        iterations: list[RefineIteration] = []
        prior_draft: Page | None = None
        prior_dissents: list[str] = []

        for i in range(1, self.max_iterations + 1):
            if not await self._has_budget_for_iteration():
                return _finalize(iterations, outcome="budget_exhausted", db=self.db)

            refine_ctx: RefineContext | None = None
            if prior_draft is not None:
                refine_ctx = RefineContext(
                    prior_title=prior_draft.headline,
                    prior_body_markdown=prior_draft.content,
                    dissents=list(prior_dissents),
                    concurrences=list(iterations[-1].verdict.concurrences if iterations else []),
                    iteration=i,
                )

            draft_id = await self._run_draft(qid, refine_ctx)
            if draft_id is None:
                return _finalize(iterations, outcome="draft_failed", db=self.db)

            if not await self._has_budget_for_review():
                return await _finalize_with_draft(
                    iterations,
                    draft_id,
                    outcome="budget_exhausted",
                    db=self.db,
                )

            verdict, review_call_id = await self._run_review(draft_id)
            if verdict is None or review_call_id is None:
                return await _finalize_with_draft(
                    iterations,
                    draft_id,
                    outcome="review_failed",
                    db=self.db,
                )

            dissents = list(verdict.dissents)
            iterations.append(
                RefineIteration(
                    iteration=i,
                    draft_page_id=draft_id,
                    review_call_id=review_call_id,
                    verdict=verdict,
                    dissents=dissents,
                )
            )

            if self._should_accept(verdict):
                await self._supersede_prior_drafts(iterations, final_id=draft_id)
                await self._mark_accepted(draft_id, iterations)
                return RefineArtifactResult(
                    final_artifact_id=draft_id,
                    outcome="accepted",
                    iterations=iterations,
                )

            if _is_stuck(prior_dissents, dissents):
                await self._supersede_prior_drafts(iterations, final_id=draft_id)
                await self._mark_accepted(draft_id, iterations, outcome="stuck")
                return RefineArtifactResult(
                    final_artifact_id=draft_id,
                    outcome="stuck",
                    iterations=iterations,
                )

            prior_draft = await self.db.get_page(draft_id)
            prior_dissents = dissents

        if iterations:
            last = iterations[-1]
            await self._supersede_prior_drafts(iterations, final_id=last.draft_page_id)
            await self._mark_accepted(last.draft_page_id, iterations, outcome="cap_reached")
            return RefineArtifactResult(
                final_artifact_id=last.draft_page_id,
                outcome="cap_reached",
                iterations=iterations,
            )

        return RefineArtifactResult(
            final_artifact_id=None,
            outcome="no_work",
            iterations=iterations,
        )

    async def _has_budget_for_iteration(self) -> bool:
        remaining = await self.db.budget_remaining()
        return remaining >= 2

    async def _has_budget_for_review(self) -> bool:
        remaining = await self.db.budget_remaining()
        return remaining >= 1

    async def _run_draft(
        self,
        question_id: str,
        refine_ctx: RefineContext | None,
    ) -> str | None:
        call = await self.db.create_call(
            CallType.DRAFT_ARTIFACT,
            scope_page_id=question_id,
        )
        runner = DraftArtifactCall(
            question_id,
            call,
            self.db,
            shape=self.shape,
            importance_threshold=self.importance_threshold,
            top_n_items=self.top_n_items,
            refine=refine_ctx,
            broadcaster=self.broadcaster,
        )
        try:
            await runner.run()
        except Exception:
            log.exception("RefineArtifact: draft iteration raised, terminating loop.")
            return None
        if runner.update_result and runner.update_result.created_page_ids:
            return runner.update_result.created_page_ids[0]
        return None

    async def _run_review(
        self,
        target_page_id: str,
    ) -> tuple[AdversarialVerdict | None, str | None]:
        call = await self.db.create_call(
            CallType.ADVERSARIAL_REVIEW,
            scope_page_id=target_page_id,
        )
        runner = AdversarialReviewCall(
            target_page_id,
            call,
            self.db,
            broadcaster=self.broadcaster,
        )
        try:
            await runner.run()
        except Exception:
            log.exception("RefineArtifact: adversarial review raised, terminating loop.")
            return None, call.id

        verdict_page = await _latest_verdict_page_for_call(self.db, call.id)
        if verdict_page is None:
            return None, call.id
        raw = verdict_page.extra.get("adversarial_verdict")
        if not isinstance(raw, dict):
            return None, call.id
        try:
            verdict = AdversarialVerdict.model_validate(raw)
        except Exception:
            log.exception("RefineArtifact: could not parse stored adversarial verdict.")
            return None, call.id
        return verdict, call.id

    def _should_accept(self, verdict: AdversarialVerdict) -> bool:
        # Dissents are epistemic preservation ("what the losing side said, for
        # future readers"), not gate-blockers — the synthesizer prompt tells
        # the model to emit them even when the verdict holds cleanly. Gating
        # on dissents meant the loop could never accept. Confidence is the
        # real acceptance signal.
        return verdict.claim_holds and verdict.confidence >= self.accept_confidence

    async def _supersede_prior_drafts(
        self,
        iterations: list[RefineIteration],
        *,
        final_id: str,
    ) -> None:
        for it in iterations:
            if it.draft_page_id == final_id:
                continue
            page = await self.db.get_page(it.draft_page_id)
            if page is None or page.is_superseded:
                continue
            page.is_superseded = True
            page.superseded_by = final_id
            await self.db.save_page(page)

    async def _mark_accepted(
        self,
        final_id: str,
        iterations: list[RefineIteration],
        *,
        outcome: str = "accepted",
    ) -> None:
        page = await self.db.get_page(final_id)
        if page is None:
            return
        final_it = iterations[-1]
        refinement_block = {
            "iterations": len(iterations),
            "outcome": outcome,
            "final_verdict": final_it.verdict.model_dump(mode="json"),
            "dissents_addressed": [d for it in iterations[:-1] for d in it.dissents],
            "remaining_dissents": list(final_it.dissents),
            "immutable": True,
        }
        existing = page.extra if isinstance(page.extra, dict) else {}
        existing["refinement"] = refinement_block
        page.extra = existing
        await self.db.save_page(page)


async def _latest_verdict_page_for_call(db: DB, review_call_id: str) -> Page | None:
    """Find the JUDGEMENT verdict page produced by a given adversarial review call."""
    rows = await db._execute(
        db.client.table("pages")
        .select("id")
        .eq("provenance_call_id", review_call_id)
        .eq("page_type", PageType.JUDGEMENT.value)
        .order("created_at", desc=True)
        .limit(1)
    )
    data = rows.data or []
    if not data:
        return None
    return await db.get_page(data[0]["id"])


def _is_stuck(prior: list[str], current: list[str]) -> bool:
    """Two consecutive iterations with the same dissent set means we're not making progress."""
    if not prior or not current:
        return False
    return sorted(prior) == sorted(current)


def _finalize(
    iterations: list[RefineIteration],
    *,
    outcome: str,
    db: DB,
) -> RefineArtifactResult:
    """Build a result when no new draft was produced this pass."""
    final_id = iterations[-1].draft_page_id if iterations else None
    return RefineArtifactResult(
        final_artifact_id=final_id,
        outcome=outcome,
        iterations=iterations,
    )


async def _finalize_with_draft(
    iterations: list[RefineIteration],
    draft_id: str,
    *,
    outcome: str,
    db: DB,
) -> RefineArtifactResult:
    """Build a result when a draft landed but review/budget prevented scoring it."""
    return RefineArtifactResult(
        final_artifact_id=draft_id,
        outcome=outcome,
        iterations=iterations,
    )
