"""Tests for RefineArtifactOrchestrator (src/rumil/orchestrators/refine_artifact.py).

The orchestrator composes DraftArtifactCall + AdversarialReviewCall in a
draft -> review -> refine loop. These tests mock both call classes at the
module boundary so zero real LLM calls are made. ``tmp_db`` provides real
Supabase I/O so page mutations / extra blocks are observable.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest_asyncio

from rumil.calls.adversarial_review import AdversarialVerdict
from rumil.calls.draft_artifact import RefineContext
from rumil.calls.stages import UpdateResult
from rumil.models import (
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)
from rumil.orchestrators.refine_artifact import (
    RefineArtifactOrchestrator,
    _is_stuck,
)


def _question() -> Page:
    return Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        headline="Will frontier AI automate routine cognitive labour by 2030?",
        abstract="Test question for refine-artifact loop.",
        content="Test question for refine-artifact loop.",
    )


def _view_item(headline: str, importance: int = 4, credence: int = 7) -> Page:
    return Page(
        page_type=PageType.VIEW_ITEM,
        layer=PageLayer.WIKI,
        workspace=Workspace.RESEARCH,
        headline=headline,
        content=f"Content for {headline}",
        credence=credence,
        robustness=3,
        importance=importance,
    )


@pytest_asyncio.fixture
async def question_with_view(tmp_db):
    """Create a question + view + view items. Mirrors the draft-artifact fixture."""
    q = _question()
    await tmp_db.save_page(q)

    view = Page(
        page_type=PageType.VIEW,
        layer=PageLayer.WIKI,
        workspace=Workspace.RESEARCH,
        headline=f"View: {q.headline}",
        content="",
        sections=["confident_views", "live_hypotheses", "key_uncertainties"],
    )
    await tmp_db.save_page(view)
    await tmp_db.save_link(
        PageLink(
            from_page_id=view.id,
            to_page_id=q.id,
            link_type=LinkType.VIEW_OF,
        )
    )

    items = [
        _view_item("Integration bottleneck dominates capability gains", importance=5, credence=7),
        _view_item("Long-horizon agency remains unreliable", importance=4, credence=6),
    ]
    for i, item in enumerate(items):
        await tmp_db.save_page(item)
        await tmp_db.save_link(
            PageLink(
                from_page_id=view.id,
                to_page_id=item.id,
                link_type=LinkType.VIEW_ITEM,
                importance=item.importance,
                section="live_hypotheses",
                position=i,
            )
        )
    return {"question": q, "view": view, "items": items}


def _verdict(
    *,
    claim_holds: bool,
    claim_confidence: int,
    dissents: Sequence[str] = (),
    concurrences: Sequence[str] = (),
    stronger_side: str = "how_true",
) -> AdversarialVerdict:
    return AdversarialVerdict(
        stronger_side=stronger_side,  # type: ignore[arg-type]
        claim_holds=claim_holds,
        claim_confidence=claim_confidence,
        rationale="Rationale synthesised for test purposes; long enough to validate.",
        dissents=list(dissents),
        concurrences=list(concurrences),
    )


def _install_draft_stub(mocker, tmp_db, question_id: str) -> dict:
    """Replace DraftArtifactCall with a fake that writes an ARTIFACT page.

    Returns a dict: ``captured["refine_contexts"]`` is the list of RefineContext
    (or None) seen across iterations; ``captured["page_ids"]`` is the list of
    artifact page IDs created in call order.
    """
    captured: dict[str, Any] = {"refine_contexts": [], "page_ids": []}

    class FakeDraftCall:
        def __init__(self, qid, call, db, **kwargs):
            self._qid = qid
            self._call = call
            self._db = db
            self._refine = kwargs.get("refine")
            self._shape = kwargs.get("shape", "strategy_brief")
            self.update_result: UpdateResult | None = None

        async def run(self):
            captured["refine_contexts"].append(self._refine)
            iteration = (self._refine.iteration if self._refine else 0) + 1
            body_suffix = (
                f"(refined iter {self._refine.iteration})"
                if self._refine is not None
                else "(initial draft)"
            )
            artifact = Page(
                page_type=PageType.ARTIFACT,
                layer=PageLayer.WIKI,
                workspace=Workspace.RESEARCH,
                headline=f"Draft iter {iteration}",
                content=f"Artifact body {body_suffix}",
                provenance_call_id=self._call.id,
                provenance_call_type=self._call.call_type.value,
                extra={"shape": self._shape},
            )
            await self._db.save_page(artifact)
            await self._db.save_link(
                PageLink(
                    from_page_id=artifact.id,
                    to_page_id=question_id,
                    link_type=LinkType.RELATED,
                    reasoning="Artifact drafted from view (test stub).",
                )
            )
            captured["page_ids"].append(artifact.id)
            # Simulate budget consumption as the real runner would (via create_call side effects).
            await self._db.consume_budget(1)
            self.update_result = UpdateResult(
                created_page_ids=[artifact.id],
                moves=[],
                all_loaded_ids=[],
                rounds_completed=1,
            )

    mocker.patch(
        "rumil.orchestrators.refine_artifact.DraftArtifactCall",
        FakeDraftCall,
    )
    return captured


def _install_review_stub(mocker, tmp_db, verdicts: Sequence[AdversarialVerdict]) -> dict:
    """Replace AdversarialReviewCall with a fake that writes a JUDGEMENT page.

    ``verdicts`` supplies one verdict per expected review call; if the orchestrator
    runs more reviews than supplied, the last verdict is reused.

    Returns ``captured["target_ids"]`` (target page ID for each review call).
    """
    captured: dict[str, Any] = {"target_ids": [], "calls": 0}

    class FakeReviewCall:
        def __init__(self, qid, call, db, **kwargs):
            self._qid = qid
            self._call = call
            self._db = db

            class _Infra:
                question_id = qid
                call = None

            self.infra = _Infra()
            self.infra.call = call

        async def run(self):
            idx = captured["calls"]
            captured["calls"] += 1
            captured["target_ids"].append(self._qid)
            v = verdicts[idx] if idx < len(verdicts) else verdicts[-1]
            verdict_page = Page(
                page_type=PageType.JUDGEMENT,
                layer=PageLayer.SQUIDGY,
                workspace=Workspace.RESEARCH,
                headline=f"Adversarial verdict for {self._qid[:8]}",
                content=v.rationale,
                credence=v.claim_confidence,
                robustness=3,
                provenance_call_id=self._call.id,
                provenance_call_type=self._call.call_type.value,
                extra={
                    "adversarial_verdict": v.model_dump(mode="json"),
                    "target_page_id": self._qid,
                },
            )
            await self._db.save_page(verdict_page)
            await self._db.save_link(
                PageLink(
                    from_page_id=verdict_page.id,
                    to_page_id=self._qid,
                    link_type=LinkType.DEPENDS_ON,
                    reasoning="Adversarial review verdict (test stub).",
                )
            )
            await self._db.consume_budget(1)

    mocker.patch(
        "rumil.orchestrators.refine_artifact.AdversarialReviewCall",
        FakeReviewCall,
    )
    return captured


def test_is_stuck_helper_detects_identical_dissent_sets():
    assert _is_stuck(["a", "b"], ["b", "a"]) is True
    assert _is_stuck(["a", "b"], ["a", "c"]) is False
    assert _is_stuck([], ["a"]) is False
    assert _is_stuck(["a"], []) is False


async def test_accepts_on_iteration_one_when_verdict_clean(tmp_db, question_with_view, mocker):
    """Clean verdict (claim_holds=True, high confidence, no dissents) accepts immediately.

    No refine call fires: the second draft stub invocation must not occur.
    """
    q = question_with_view["question"]
    draft_cap = _install_draft_stub(mocker, tmp_db, q.id)
    _install_review_stub(
        mocker,
        tmp_db,
        [_verdict(claim_holds=True, claim_confidence=8)],
    )

    orch = RefineArtifactOrchestrator(
        tmp_db,
        question_id=q.id,
        shape="strategy_brief",
        max_iterations=3,
        accept_confidence=6,
    )
    result = await orch.run()

    assert result.outcome == "accepted"
    assert result.iteration_count == 1
    assert result.final_artifact_id is not None
    assert len(draft_cap["page_ids"]) == 1
    # No refine context was passed — this was a first draft.
    assert draft_cap["refine_contexts"] == [None]


async def test_accepts_when_confidence_clears_even_with_surviving_dissents(
    tmp_db, question_with_view, mocker
):
    """Dissents are epistemic preservation, not acceptance blockers.

    The synthesizer prompt asks for dissents "even when you are confident in
    the verdict", so gating on `not dissents` would mean the loop never
    accepts. Confidence is the real acceptance signal.
    """
    q = question_with_view["question"]
    _install_draft_stub(mocker, tmp_db, q.id)
    _install_review_stub(
        mocker,
        tmp_db,
        [
            _verdict(
                claim_holds=True,
                claim_confidence=8,
                dissents=["one surviving objection the losing side raised"],
            )
        ],
    )

    orch = RefineArtifactOrchestrator(
        tmp_db,
        question_id=q.id,
        shape="strategy_brief",
        max_iterations=3,
        accept_confidence=7,
    )
    result = await orch.run()

    assert result.outcome == "accepted"
    assert result.iteration_count == 1


async def test_claim_confidence_and_dissents_are_independent_gate_signals(
    tmp_db, question_with_view, mocker
):
    """Regression for the conflation bug documented in marketplace-thread/32.

    Before the schema split, the synthesizer entangled "claim confidence" with
    "are there surviving dissents" and clamped `confidence=6` across the board
    (9/9 verdicts in smoke-test runs). After the split, `claim_confidence` is
    the bet-on-the-claim signal and is independent of `dissents`: a verdict
    with multiple surviving dissents can still ship `claim_confidence=8` and
    must be accepted above the default threshold of 6.
    """
    q = question_with_view["question"]
    _install_draft_stub(mocker, tmp_db, q.id)
    _install_review_stub(
        mocker,
        tmp_db,
        [
            _verdict(
                claim_holds=True,
                claim_confidence=8,
                dissents=[
                    "param-count proxy critique",
                    "alternative scaling-path hypothesis",
                ],
            )
        ],
    )

    orch = RefineArtifactOrchestrator(
        tmp_db,
        question_id=q.id,
        shape="strategy_brief",
        max_iterations=3,
        accept_confidence=6,
    )
    result = await orch.run()

    assert result.outcome == "accepted"
    assert result.iteration_count == 1
    final = await tmp_db.get_page(result.final_artifact_id)  # type: ignore[arg-type]
    assert final is not None
    block = final.extra["refinement"]
    assert block["final_verdict"]["claim_confidence"] == 8
    assert block["remaining_dissents"] == [
        "param-count proxy critique",
        "alternative scaling-path hypothesis",
    ]


async def test_accepts_on_iteration_two_after_refine(tmp_db, question_with_view, mocker):
    """Draft with dissents triggers a refine pass; second review is clean -> accept."""
    q = question_with_view["question"]
    draft_cap = _install_draft_stub(mocker, tmp_db, q.id)
    review_cap = _install_review_stub(
        mocker,
        tmp_db,
        [
            _verdict(
                claim_holds=False,
                claim_confidence=5,
                dissents=["missed the integration bottleneck", "forecasts underspecified"],
                concurrences=["good framing of scale trends"],
            ),
            _verdict(
                claim_holds=True, claim_confidence=7, concurrences=["addressed dissents cleanly"]
            ),
        ],
    )

    orch = RefineArtifactOrchestrator(
        tmp_db,
        question_id=q.id,
        shape="strategy_brief",
        max_iterations=3,
        accept_confidence=6,
    )
    result = await orch.run()

    assert result.outcome == "accepted"
    assert result.iteration_count == 2
    assert review_cap["calls"] == 2
    # First draft has no refine context; second has one carrying the dissents + concurrences.
    assert draft_cap["refine_contexts"][0] is None
    second_ctx = draft_cap["refine_contexts"][1]
    assert isinstance(second_ctx, RefineContext)
    assert second_ctx.iteration == 2
    assert "missed the integration bottleneck" in second_ctx.dissents
    assert "good framing of scale trends" in second_ctx.concurrences
    # The first draft should now be superseded by the accepted one.
    first_id, second_id = draft_cap["page_ids"]
    assert result.final_artifact_id == second_id
    first_page = await tmp_db.get_page(first_id)
    assert first_page is not None
    assert first_page.is_superseded is True
    assert first_page.superseded_by == second_id


async def test_stuck_when_dissents_repeat_unchanged(tmp_db, question_with_view, mocker):
    """Two consecutive iterations with the same dissent set terminates as 'stuck'."""
    q = question_with_view["question"]
    repeated = ["unaddressed crux A", "unaddressed crux B"]
    draft_cap = _install_draft_stub(mocker, tmp_db, q.id)
    _install_review_stub(
        mocker,
        tmp_db,
        [
            _verdict(claim_holds=False, claim_confidence=4, dissents=list(repeated)),
            _verdict(claim_holds=False, claim_confidence=4, dissents=list(reversed(repeated))),
        ],
    )

    orch = RefineArtifactOrchestrator(
        tmp_db,
        question_id=q.id,
        shape="strategy_brief",
        max_iterations=5,
        accept_confidence=6,
    )
    result = await orch.run()

    assert result.outcome == "stuck"
    assert result.iteration_count == 2
    final = await tmp_db.get_page(result.final_artifact_id)  # type: ignore[arg-type]
    assert final is not None
    refinement = final.extra.get("refinement")
    assert refinement is not None
    assert refinement["outcome"] == "stuck"
    # Two draft passes before giving up.
    assert len(draft_cap["page_ids"]) == 2


async def test_iteration_cap_reached_saves_last_draft(tmp_db, question_with_view, mocker):
    """When the cap is hit with perpetually shifting dissents, last draft is saved."""
    q = question_with_view["question"]
    _install_draft_stub(mocker, tmp_db, q.id)
    _install_review_stub(
        mocker,
        tmp_db,
        [
            _verdict(claim_holds=False, claim_confidence=4, dissents=["a1"]),
            _verdict(claim_holds=False, claim_confidence=4, dissents=["a2"]),
            _verdict(claim_holds=False, claim_confidence=4, dissents=["a3"]),
        ],
    )

    orch = RefineArtifactOrchestrator(
        tmp_db,
        question_id=q.id,
        shape="strategy_brief",
        max_iterations=3,
        accept_confidence=6,
    )
    result = await orch.run()

    assert result.outcome == "cap_reached"
    assert result.iteration_count == 3
    final = await tmp_db.get_page(result.final_artifact_id)  # type: ignore[arg-type]
    assert final is not None
    assert final.extra["refinement"]["outcome"] == "cap_reached"


async def test_budget_exhaustion_mid_loop_terminates(tmp_db, question_with_view, mocker):
    """Budget of 2 allows exactly one iteration (draft + review). Second iteration's
    pre-check sees insufficient budget and terminates without accepting."""
    q = question_with_view["question"]
    await tmp_db.init_budget(2)

    draft_cap = _install_draft_stub(mocker, tmp_db, q.id)
    _install_review_stub(
        mocker,
        tmp_db,
        [
            _verdict(claim_holds=False, claim_confidence=4, dissents=["d1"]),
            _verdict(claim_holds=True, claim_confidence=8),
        ],
    )

    orch = RefineArtifactOrchestrator(
        tmp_db,
        question_id=q.id,
        shape="strategy_brief",
        max_iterations=5,
        accept_confidence=6,
    )
    result = await orch.run()

    assert result.outcome == "budget_exhausted"
    # Exactly one draft + one review consumed the budget.
    assert len(draft_cap["page_ids"]) == 1
    assert result.final_artifact_id == draft_cap["page_ids"][0]


async def test_refinement_extra_block_populated_on_accept(tmp_db, question_with_view, mocker):
    """Accepted artifacts carry extra['refinement'] with iterations, verdict, dissents_addressed."""
    q = question_with_view["question"]
    draft_cap = _install_draft_stub(mocker, tmp_db, q.id)
    first_dissents = ["missed bottleneck", "weak forecast"]
    _install_review_stub(
        mocker,
        tmp_db,
        [
            _verdict(claim_holds=False, claim_confidence=5, dissents=list(first_dissents)),
            _verdict(claim_holds=True, claim_confidence=8, concurrences=["clean revision"]),
        ],
    )

    orch = RefineArtifactOrchestrator(
        tmp_db,
        question_id=q.id,
        shape="strategy_brief",
        max_iterations=3,
        accept_confidence=6,
    )
    result = await orch.run()

    assert result.outcome == "accepted"
    final = await tmp_db.get_page(result.final_artifact_id)  # type: ignore[arg-type]
    assert final is not None
    block = final.extra["refinement"]
    assert block["iterations"] == 2
    assert block["outcome"] == "accepted"
    assert block["immutable"] is True
    # Dissents from earlier iterations were captured as 'addressed'.
    assert set(block["dissents_addressed"]) == set(first_dissents)
    # Final verdict was the clean one.
    assert block["final_verdict"]["claim_holds"] is True
    assert block["final_verdict"]["claim_confidence"] == 8
    # Remaining dissents on the accepted verdict should be empty.
    assert block["remaining_dissents"] == []
    # And only the accepted draft is the 'active' artifact.
    assert draft_cap["page_ids"][-1] == final.id
