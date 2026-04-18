"""Adversarial Review call: wrapper that runs how-true + how-false scouts on a
target claim and synthesizes a single structured verdict from the two outputs.

This is Tier-0 scaffolding for Owen's bolded adversarial-review priority in
`marketplace-thread/17-worldview-update-frame.md`. It does NOT apply the verdict
to a View, decide promotion, or feed back into any orchestrator — it just
produces a verdict page and links it to the target. Wiring is deferred.

The call has three phases that collapse into the standard CallRunner shape:
  1. build_context: load the target claim and existing context.
  2. update_workspace: dispatch how-true + how-false scouts in parallel,
     collect the pages they produced, run the synthesizer LLM to produce a
     structured verdict, and persist the verdict as a JUDGEMENT page linked
     to the target with DEPENDS_ON.
  3. closing_review: standard closing review.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from rumil.calls.closing_reviewers import StandardClosingReview
from rumil.calls.common import resolve_page_refs
from rumil.calls.scout_c_how_false import ScoutCHowFalseCall
from rumil.calls.scout_c_how_true import ScoutCHowTrueCall
from rumil.calls.stages import (
    CallInfra,
    CallRunner,
    ClosingReviewer,
    ContextBuilder,
    ContextResult,
    UpdateResult,
    WorkspaceUpdater,
)
from rumil.context import format_page
from rumil.database import DB
from rumil.llm import LLMExchangeMetadata, _load_file, structured_call
from rumil.models import (
    Call,
    CallType,
    LinkType,
    Page,
    PageDetail,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)
from rumil.tracing.trace_events import ContextBuiltEvent

log = logging.getLogger(__name__)


SYNTHESIZER_PROMPT_FILE = "adversarial_review_synthesizer.md"

SCOUT_MAX_ROUNDS = 2
SCOUT_FRUIT_THRESHOLD = 4


StrongerSide = Literal["how_true", "how_false", "tie"]


class AdversarialVerdict(BaseModel):
    """Structured output of the synthesizer LLM."""

    stronger_side: StrongerSide = Field(
        description=(
            "Which scout produced the stronger overall case. Use 'tie' only "
            "when the two sides are genuinely balanced, not as a hedge."
        )
    )
    claim_holds: bool = Field(
        description=(
            "After weighing both sides, does the claim hold? This may, but "
            "need not, agree with stronger_side."
        )
    )
    confidence: int = Field(
        ge=1,
        le=9,
        description="Rumil-style credence on the verdict (1-9, 5 = genuinely uncertain).",
    )
    rationale: str = Field(
        description=(
            "One paragraph (4-8 sentences). Name the strongest point on each "
            "side, say why one outweighs the other, and flag any unresolved "
            "cruxes."
        )
    )
    concurrences: list[str] = Field(
        default_factory=list,
        description=(
            "1-3 concurring points: arguments from the winning side that "
            "weren't its primary thrust — things the winning side could have "
            "argued but didn't. Preserved for future reviewers."
        ),
    )
    dissents: list[str] = Field(
        default_factory=list,
        description=(
            "1-3 dissenting points: surviving arguments from the losing side "
            "that still have merit. A careful reader should know these even "
            "if the verdict went the other way."
        ),
    )
    sunset_after_days: int | None = Field(
        default=None,
        description=(
            "Shelf life of this verdict in days. Fast-moving empirical claims: "
            "30. Medium-stability: 180. Structural/definitional claims that "
            "don't depend on changing evidence: null (never expires). After "
            "this window, the verdict should be re-reviewed."
        ),
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="When the verdict was synthesized. Used for sunset expiry.",
    )


def is_verdict_expired(
    verdict: AdversarialVerdict,
    now: datetime | None = None,
) -> bool:
    """Return True if *verdict* has passed its sunset window.

    A verdict with ``sunset_after_days is None`` never expires (structural
    claims). Otherwise the verdict is expired when
    ``now > verdict.created_at + sunset_after_days``.
    """
    if verdict.sunset_after_days is None:
        return False
    now = now or datetime.now(UTC)
    created = verdict.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return now > created + timedelta(days=verdict.sunset_after_days)


class AdversarialReviewContext(ContextBuilder):
    """Minimal context: just the target page rendered at content depth."""

    async def build_context(self, infra: CallInfra) -> ContextResult:
        target = await infra.db.get_page(infra.question_id)
        if target is None:
            raise ValueError(f"AdversarialReview: target page {infra.question_id!r} not found.")

        rendered = await format_page(
            target,
            PageDetail.CONTENT,
            linked_detail=None,
            db=infra.db,
            track=True,
            track_tags={"source": "adversarial_review_target"},
        )
        context_text = (
            f"## Target ({target.page_type.value.upper()}) `{target.id[:8]}`\n\n{rendered}\n"
        )

        refs = await resolve_page_refs([target.id], infra.db)
        await infra.trace.record(
            ContextBuiltEvent(
                working_context_page_ids=refs,
                preloaded_page_ids=[],
                source_page_id=None,
                scout_mode=None,
            )
        )
        return ContextResult(
            context_text=context_text,
            working_page_ids=[target.id],
            preloaded_ids=[],
        )


async def _run_scout(
    scout_cls: type[CallRunner],
    scout_call_type: CallType,
    target_id: str,
    db: DB,
    *,
    parent_call_id: str,
    broadcaster,
    max_rounds: int,
    fruit_threshold: int,
) -> str:
    """Create + run a scout call against *target_id*. Returns the scout call id."""
    scout_call = await db.create_call(
        scout_call_type,
        scope_page_id=target_id,
        parent_call_id=parent_call_id,
    )
    runner = scout_cls(
        target_id,
        scout_call,
        db,
        broadcaster=broadcaster,
        max_rounds=max_rounds,
        fruit_threshold=fruit_threshold,
    )
    await runner.run()
    return scout_call.id


async def _render_scout_output(
    scout_call_id: str,
    db: DB,
    heading: str,
) -> str:
    """Render all pages a scout produced as markdown for the synthesizer prompt."""
    rows = await db._execute(
        db.client.table("pages")
        .select("id")
        .eq("provenance_call_id", scout_call_id)
        .eq("is_superseded", False)
        .order("created_at")
    )
    page_ids = [r["id"] for r in (rows.data or [])]

    if not page_ids:
        return f"## {heading}\n\n(no pages produced)\n"

    pages = await db.get_pages_by_ids(page_ids)
    parts: list[str] = [f"## {heading}\n"]
    for pid in page_ids:
        page = pages.get(pid)
        if page is None:
            continue
        rendered = await format_page(
            page,
            PageDetail.CONTENT,
            linked_detail=None,
            db=db,
            track=False,
        )
        parts.append(
            f"\n### [{page.page_type.value.upper()}] "
            f"`{page.id[:8]}` — {page.headline}\n\n{rendered}\n"
        )
    return "\n".join(parts)


async def _persist_verdict(
    verdict: AdversarialVerdict,
    target: Page,
    call: Call,
    db: DB,
) -> str:
    """Write the verdict as a JUDGEMENT page and link it to the target.

    Returns the created page id. The link is DEPENDS_ON — the verdict
    depends on (is about) the target claim. We do NOT use ANSWERS since
    this is not the canonical answer to a research question; it is an
    adjudicated opinion on a single claim.
    """
    claim_holds_word = "holds" if verdict.claim_holds else "does not hold"
    side_word = {
        "how_true": "how-true scout stronger",
        "how_false": "how-false scout stronger",
        "tie": "both sides balanced",
    }[verdict.stronger_side]
    headline = (
        f"Adversarial verdict: {target.headline[:80]} — {claim_holds_word} "
        f"(C{verdict.confidence}; {side_word})"
    )
    content = (
        f"**Claim holds:** {verdict.claim_holds}  \n"
        f"**Stronger side:** {verdict.stronger_side}  \n"
        f"**Confidence:** {verdict.confidence}\n\n"
        f"{verdict.rationale}"
    )
    page = Page(
        page_type=PageType.JUDGEMENT,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=content,
        headline=headline[:200],
        credence=verdict.confidence,
        robustness=3,
        provenance_model="adversarial_review_synthesizer",
        provenance_call_type=call.call_type.value,
        provenance_call_id=call.id,
        extra={
            "adversarial_verdict": verdict.model_dump(mode="json"),
            "target_page_id": target.id,
        },
    )
    await db.save_page(page)
    await db.save_link(
        PageLink(
            from_page_id=page.id,
            to_page_id=target.id,
            link_type=LinkType.DEPENDS_ON,
            reasoning="Adversarial review verdict targets this claim.",
        )
    )
    log.info(
        "AdversarialReview: verdict page %s created for target %s (holds=%s, conf=%d)",
        page.id[:8],
        target.id[:8],
        verdict.claim_holds,
        verdict.confidence,
    )
    return page.id


class AdversarialReviewUpdater(WorkspaceUpdater):
    """Dispatch how-true + how-false scouts, synthesize the verdict, persist it."""

    def __init__(
        self,
        *,
        scout_max_rounds: int = SCOUT_MAX_ROUNDS,
        scout_fruit_threshold: int = SCOUT_FRUIT_THRESHOLD,
        broadcaster=None,
    ) -> None:
        self._scout_max_rounds = scout_max_rounds
        self._scout_fruit_threshold = scout_fruit_threshold
        self._broadcaster = broadcaster

    async def update_workspace(
        self,
        infra: CallInfra,
        context: ContextResult,
    ) -> UpdateResult:
        target_id = infra.question_id
        target = await infra.db.get_page(target_id)
        if target is None:
            raise ValueError(
                f"AdversarialReview: target page {target_id!r} disappeared "
                "between context_build and update_workspace."
            )

        how_true_id, how_false_id = await asyncio.gather(
            _run_scout(
                ScoutCHowTrueCall,
                CallType.SCOUT_C_HOW_TRUE,
                target_id,
                infra.db,
                parent_call_id=infra.call.id,
                broadcaster=self._broadcaster,
                max_rounds=self._scout_max_rounds,
                fruit_threshold=self._scout_fruit_threshold,
            ),
            _run_scout(
                ScoutCHowFalseCall,
                CallType.SCOUT_C_HOW_FALSE,
                target_id,
                infra.db,
                parent_call_id=infra.call.id,
                broadcaster=self._broadcaster,
                max_rounds=self._scout_max_rounds,
                fruit_threshold=self._scout_fruit_threshold,
            ),
        )

        how_true_md = await _render_scout_output(how_true_id, infra.db, "How-True Scout Output")
        how_false_md = await _render_scout_output(how_false_id, infra.db, "How-False Scout Output")

        synth_user = (
            f"{context.context_text}\n\n---\n\n"
            f"{how_true_md}\n\n---\n\n"
            f"{how_false_md}\n\n---\n\n"
            "Produce your structured verdict now."
        )
        synth_system = _load_file(SYNTHESIZER_PROMPT_FILE)

        synth_meta = LLMExchangeMetadata(
            call_id=infra.call.id,
            phase="update_workspace",
            user_message=synth_user,
        )
        synth_result = await structured_call(
            system_prompt=synth_system,
            user_message=synth_user,
            response_model=AdversarialVerdict,
            metadata=synth_meta,
            db=infra.db,
        )
        if synth_result.parsed is None:
            raise ValueError("AdversarialReview: synthesizer returned no parseable verdict.")
        verdict: AdversarialVerdict = synth_result.parsed

        verdict_page_id = await _persist_verdict(verdict, target, infra.call, infra.db)
        infra.state.created_page_ids.append(verdict_page_id)

        return UpdateResult(
            created_page_ids=[verdict_page_id],
            moves=[],
            all_loaded_ids=list(context.working_page_ids),
            rounds_completed=1,
        )


class AdversarialReviewCall(CallRunner):
    """Wrap how-true + how-false scouts with a synthesizing adjudicator.

    The call's *question_id* is the id of the target — typically a CLAIM or
    JUDGEMENT page — that the scouts will examine. This is Tier-0 scaffolding:
    it produces a verdict page but nothing downstream consumes it yet.
    """

    context_builder_cls = AdversarialReviewContext
    workspace_updater_cls = AdversarialReviewUpdater
    closing_reviewer_cls = StandardClosingReview
    call_type = CallType.ADVERSARIAL_REVIEW

    def __init__(
        self,
        question_id: str,
        call: Call,
        db: DB,
        *,
        broadcaster=None,
        up_to_stage=None,
        max_rounds: int = 1,
        fruit_threshold: int = 4,
        scout_max_rounds: int = SCOUT_MAX_ROUNDS,
        scout_fruit_threshold: int = SCOUT_FRUIT_THRESHOLD,
    ) -> None:
        self._scout_max_rounds = scout_max_rounds
        self._scout_fruit_threshold = scout_fruit_threshold
        self._broadcaster = broadcaster
        super().__init__(
            question_id,
            call,
            db,
            broadcaster=broadcaster,
            up_to_stage=up_to_stage,
            max_rounds=max_rounds,
            fruit_threshold=fruit_threshold,
        )

    def _make_context_builder(self) -> ContextBuilder:
        return AdversarialReviewContext()

    def _make_workspace_updater(self) -> WorkspaceUpdater:
        return AdversarialReviewUpdater(
            scout_max_rounds=self._scout_max_rounds,
            scout_fruit_threshold=self._scout_fruit_threshold,
            broadcaster=self._broadcaster,
        )

    def _make_closing_reviewer(self) -> ClosingReviewer:
        return StandardClosingReview(self.call_type)

    def task_description(self) -> str:
        return (
            "Run an adversarial review of the target claim: dispatch a "
            "how-true scout and a how-false scout in parallel, then "
            "synthesize a single structured verdict (stronger side, does "
            "the claim hold, confidence, rationale) from their outputs.\n\n"
            f"Target ID: `{self.infra.question_id}`"
        )
