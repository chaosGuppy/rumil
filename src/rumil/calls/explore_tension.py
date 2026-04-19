"""ExploreTension call: adjudicate a tracked tension between two claims.

Given two high-credence claims in tension on a shared question, runs a
how-true scout on claim A and a how-false scout on claim B in parallel,
then synthesizes a structured verdict: which claim survives, whether the
tension dissolves under a refining distinction, or whether the
disagreement is genuinely unresolved.

This is the workspace-mutating counterpart to ``tensions.py`` (which only
*detects* tensions). Structurally very close to ``adversarial_review.py``
— the key differences are:

- scope covers *two* target claims, not one, and a shared question
- verdict semantics: resolution + optional refining claim, not a pure
  "claim holds" / "doesn't hold" boolean
- persists two RELATED links (one to each tension claim) plus an ANSWERS
  link to the parent question if a refining claim is produced
- the verdict page is tagged with ``extra.tension_pair`` so
  ``tensions.unexplored_tension_candidates`` can skip already-adjudicated
  pairs on subsequent orchestrator iterations

Not dispatchable from prioritization — fired by the
``TensionExplorationPolicy`` (or manually).
"""

from __future__ import annotations

import asyncio
import logging
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


EXPLORE_TENSION_PROMPT_FILE = "explore_tension.md"

SCOUT_MAX_ROUNDS = 2
SCOUT_FRUIT_THRESHOLD = 4

Resolution = Literal[
    "a_survives",
    "b_survives",
    "both_survive_refined",
    "genuine_disagreement",
]


class TensionVerdict(BaseModel):
    """Structured output of the tension-exploration synthesizer LLM."""

    resolution: Resolution = Field(
        description=(
            "How the tension resolves. 'a_survives' / 'b_survives' pick a "
            "winner; 'both_survive_refined' means a distinction dissolves "
            "the conflict; 'genuine_disagreement' means the evidence leaves "
            "the tension real and unresolved."
        )
    )
    rationale: str = Field(
        description=(
            "3-6 sentences naming the strongest point on each side and why "
            "one side outweighs the other (or why neither does)."
        )
    )
    refining_claim_headline: str | None = Field(
        default=None,
        description=(
            "Headline for a new refining claim to write into the workspace. "
            "Required when resolution is 'both_survive_refined'; null "
            "otherwise."
        ),
    )
    refining_claim_content: str | None = Field(
        default=None,
        description=(
            "Full content for the refining claim. Required when resolution "
            "is 'both_survive_refined'; null otherwise."
        ),
    )
    confidence: int = Field(
        ge=1,
        le=9,
        description="Rumil-style credence on the verdict (1-9, 5 = genuinely uncertain).",
    )


def _parse_tension_pair(call: Call) -> tuple[str, str, str]:
    """Extract (question_id, claim_a_id, claim_b_id) from call_params.

    The explorer is always instantiated with these IDs in call_params — it
    targets a specific triple that the tension policy picked. We raise
    rather than try to reconstruct them, since the whole point of the call
    is that triple.
    """
    params = call.call_params or {}
    qid = params.get("tension_question_id")
    a = params.get("tension_claim_a_id")
    b = params.get("tension_claim_b_id")
    if not (qid and a and b):
        raise ValueError(
            "ExploreTensionCall: call.call_params must contain "
            "tension_question_id, tension_claim_a_id, tension_claim_b_id."
        )
    return str(qid), str(a), str(b)


class ExploreTensionContext(ContextBuilder):
    """Load the question + both tension claims at CONTENT depth."""

    async def build_context(self, infra: CallInfra) -> ContextResult:
        question_id, claim_a_id, claim_b_id = _parse_tension_pair(infra.call)

        pages = await infra.db.get_pages_by_ids([question_id, claim_a_id, claim_b_id])
        question = pages.get(question_id)
        claim_a = pages.get(claim_a_id)
        claim_b = pages.get(claim_b_id)
        missing = [
            label
            for label, page in (("question", question), ("claim_a", claim_a), ("claim_b", claim_b))
            if page is None
        ]
        if missing:
            raise ValueError(
                f"ExploreTension: missing pages ({', '.join(missing)}) for call "
                f"{infra.call.id[:8]}."
            )
        assert question is not None and claim_a is not None and claim_b is not None

        question_md = await format_page(
            question,
            PageDetail.CONTENT,
            linked_detail=None,
            db=infra.db,
            track=True,
            track_tags={"source": "explore_tension_question"},
        )
        claim_a_md = await format_page(
            claim_a,
            PageDetail.CONTENT,
            linked_detail=None,
            db=infra.db,
            track=True,
            track_tags={"source": "explore_tension_claim_a"},
        )
        claim_b_md = await format_page(
            claim_b,
            PageDetail.CONTENT,
            linked_detail=None,
            db=infra.db,
            track=True,
            track_tags={"source": "explore_tension_claim_b"},
        )

        context_text = (
            f"## Question `{question.id[:8]}`\n\n{question_md}\n\n"
            f"## Claim A `{claim_a.id[:8]}` "
            f"(credence={claim_a.credence})\n\n{claim_a_md}\n\n"
            f"## Claim B `{claim_b.id[:8]}` "
            f"(credence={claim_b.credence})\n\n{claim_b_md}\n"
        )

        refs = await resolve_page_refs([question.id, claim_a.id, claim_b.id], infra.db)
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
            working_page_ids=[question.id, claim_a.id, claim_b.id],
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


async def _render_scout_output(scout_call_id: str, db: DB, heading: str) -> str:
    """Render all pages a scout produced as markdown for the synthesizer."""
    query = (
        db.client.table("pages")
        .select("id")
        .eq("provenance_call_id", scout_call_id)
        .eq("is_superseded", False)
        .order("created_at")
    )
    # TODO: event-replay overlay (see CLAUDE.md staged-runs section)
    rows = await db._execute(db._staged_filter(query))
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
            page, PageDetail.CONTENT, linked_detail=None, db=db, track=False
        )
        parts.append(
            f"\n### [{page.page_type.value.upper()}] "
            f"`{page.id[:8]}` — {page.headline}\n\n{rendered}\n"
        )
    return "\n".join(parts)


async def _persist_verdict(
    verdict: TensionVerdict,
    question: Page,
    claim_a: Page,
    claim_b: Page,
    call: Call,
    db: DB,
) -> tuple[str, str | None]:
    """Write the verdict as a JUDGEMENT page + optional refining claim.

    Returns ``(verdict_page_id, refining_claim_id | None)``. The verdict
    page is linked to both tension claims with LinkType.RELATED so the
    tension-dedup logic in ``tensions._already_explored_pair_keys`` can
    find the pair.
    """
    resolution_word = {
        "a_survives": "A survives",
        "b_survives": "B survives",
        "both_survive_refined": "both survive (refined)",
        "genuine_disagreement": "genuine disagreement",
    }[verdict.resolution]
    headline = (
        f"Tension verdict: {claim_a.headline[:40]} vs "
        f"{claim_b.headline[:40]} — {resolution_word} (C{verdict.confidence})"
    )[:200]
    content = (
        f"**Resolution:** {verdict.resolution}  \n"
        f"**Confidence:** {verdict.confidence}\n\n"
        f"{verdict.rationale}\n"
    )
    verdict_page = Page(
        page_type=PageType.JUDGEMENT,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=content,
        headline=headline,
        credence=verdict.confidence,
        robustness=3,
        provenance_model="explore_tension_synthesizer",
        provenance_call_type=call.call_type.value,
        provenance_call_id=call.id,
        extra={
            "tension_verdict": verdict.model_dump(mode="json"),
            "tension_pair": {
                "question_id": question.id,
                "claim_a_id": claim_a.id,
                "claim_b_id": claim_b.id,
            },
        },
    )
    await db.save_page(verdict_page)

    await db.save_link(
        PageLink(
            from_page_id=verdict_page.id,
            to_page_id=claim_a.id,
            link_type=LinkType.RELATED,
            reasoning=(
                f"Tension verdict: {verdict.resolution}. This judgement "
                "adjudicates a tracked tension with the other linked claim."
            ),
        )
    )
    await db.save_link(
        PageLink(
            from_page_id=verdict_page.id,
            to_page_id=claim_b.id,
            link_type=LinkType.RELATED,
            reasoning=(
                f"Tension verdict: {verdict.resolution}. This judgement "
                "adjudicates a tracked tension with the other linked claim."
            ),
        )
    )

    refining_id: str | None = None
    if (
        verdict.resolution == "both_survive_refined"
        and verdict.refining_claim_headline
        and verdict.refining_claim_content
    ):
        refining = Page(
            page_type=PageType.CLAIM,
            layer=PageLayer.SQUIDGY,
            workspace=Workspace.RESEARCH,
            content=verdict.refining_claim_content,
            headline=verdict.refining_claim_headline[:200],
            credence=verdict.confidence,
            robustness=3,
            provenance_model="explore_tension_synthesizer",
            provenance_call_type=call.call_type.value,
            provenance_call_id=call.id,
            extra={
                "refines_tension": {
                    "question_id": question.id,
                    "claim_a_id": claim_a.id,
                    "claim_b_id": claim_b.id,
                    "verdict_page_id": verdict_page.id,
                },
            },
        )
        await db.save_page(refining)
        await db.save_link(
            PageLink(
                from_page_id=refining.id,
                to_page_id=question.id,
                link_type=LinkType.CONSIDERATION,
                reasoning="Refining claim produced by tension exploration.",
            )
        )
        refining_id = refining.id

    log.info(
        "ExploreTension: verdict %s created for tension q=%s a=%s b=%s (resolution=%s, conf=%d)",
        verdict_page.id[:8],
        question.id[:8],
        claim_a.id[:8],
        claim_b.id[:8],
        verdict.resolution,
        verdict.confidence,
    )
    return verdict_page.id, refining_id


class ExploreTensionUpdater(WorkspaceUpdater):
    """Dispatch how-true(A) + how-false(B) scouts, synthesize a tension verdict."""

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
        question_id, claim_a_id, claim_b_id = _parse_tension_pair(infra.call)
        pages = await infra.db.get_pages_by_ids([question_id, claim_a_id, claim_b_id])
        question = pages.get(question_id)
        claim_a = pages.get(claim_a_id)
        claim_b = pages.get(claim_b_id)
        if not (question and claim_a and claim_b):
            raise ValueError(
                "ExploreTension: tension pages disappeared between "
                "build_context and update_workspace."
            )

        how_true_id, how_false_id = await asyncio.gather(
            _run_scout(
                ScoutCHowTrueCall,
                CallType.SCOUT_C_HOW_TRUE,
                claim_a.id,
                infra.db,
                parent_call_id=infra.call.id,
                broadcaster=self._broadcaster,
                max_rounds=self._scout_max_rounds,
                fruit_threshold=self._scout_fruit_threshold,
            ),
            _run_scout(
                ScoutCHowFalseCall,
                CallType.SCOUT_C_HOW_FALSE,
                claim_b.id,
                infra.db,
                parent_call_id=infra.call.id,
                broadcaster=self._broadcaster,
                max_rounds=self._scout_max_rounds,
                fruit_threshold=self._scout_fruit_threshold,
            ),
        )

        how_true_md = await _render_scout_output(how_true_id, infra.db, "How-True Scout on Claim A")
        how_false_md = await _render_scout_output(
            how_false_id, infra.db, "How-False Scout on Claim B"
        )

        synth_user = (
            f"{context.context_text}\n\n---\n\n"
            f"{how_true_md}\n\n---\n\n"
            f"{how_false_md}\n\n---\n\n"
            "Produce your structured tension verdict now."
        )
        synth_system = _load_file(EXPLORE_TENSION_PROMPT_FILE)

        synth_meta = LLMExchangeMetadata(
            call_id=infra.call.id,
            phase="update_workspace",
            user_message=synth_user,
        )
        synth_result = await structured_call(
            system_prompt=synth_system,
            user_message=synth_user,
            response_model=TensionVerdict,
            metadata=synth_meta,
            db=infra.db,
        )
        if synth_result.parsed is None:
            raise ValueError("ExploreTension: synthesizer returned no parseable verdict.")
        verdict: TensionVerdict = synth_result.parsed

        verdict_id, refining_id = await _persist_verdict(
            verdict, question, claim_a, claim_b, infra.call, infra.db
        )
        created_ids = [verdict_id]
        if refining_id is not None:
            created_ids.append(refining_id)
        infra.state.created_page_ids.extend(created_ids)

        return UpdateResult(
            created_page_ids=created_ids,
            moves=[],
            all_loaded_ids=list(context.working_page_ids),
            rounds_completed=1,
        )


class ExploreTensionCall(CallRunner):
    """Adjudicate a tracked tension between two claims on a shared question.

    ``call.call_params`` must contain ``tension_question_id``,
    ``tension_claim_a_id``, ``tension_claim_b_id``. The ``question_id``
    positional arg (inherited from ``CallRunner``) is set to the parent
    question for consistency with the rest of the call surface, but the
    workspace updater reads the triple from ``call_params`` directly.
    """

    context_builder_cls = ExploreTensionContext
    workspace_updater_cls = ExploreTensionUpdater
    closing_reviewer_cls = StandardClosingReview
    call_type = CallType.EXPLORE_TENSION

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
        return ExploreTensionContext()

    def _make_workspace_updater(self) -> WorkspaceUpdater:
        return ExploreTensionUpdater(
            scout_max_rounds=self._scout_max_rounds,
            scout_fruit_threshold=self._scout_fruit_threshold,
            broadcaster=self._broadcaster,
        )

    def _make_closing_reviewer(self) -> ClosingReviewer:
        return StandardClosingReview(self.call_type)

    def task_description(self) -> str:
        qid, a, b = _parse_tension_pair(self.infra.call)
        return (
            "Adjudicate a tracked tension between two high-credence claims "
            "on a shared question: dispatch a how-true scout on claim A and "
            "a how-false scout on claim B in parallel, then synthesize a "
            "structured verdict (resolution, rationale, optional refining "
            "claim, confidence) from their outputs.\n\n"
            f"Question ID: `{qid}`\n"
            f"Claim A ID: `{a}`\n"
            f"Claim B ID: `{b}`"
        )
