"""Tests for proposer-review mechanics added to adversarial review:
concurrence/dissent preservation and sunset provisions.

Per `marketplace-thread/23-proposer-review-wild.md`. All tests mock the LLM
(zero API calls) — the synthesizer's structured output is stubbed directly.
"""

from datetime import UTC, datetime, timedelta

import pytest_asyncio

from rumil.calls.adversarial_review import (
    AdversarialReviewUpdater,
    AdversarialVerdict,
    is_verdict_expired,
)
from rumil.calls.stages import CallInfra, ContextResult
from rumil.llm import StructuredCallResult
from rumil.models import (
    Call,
    CallStatus,
    CallType,
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)
from rumil.moves.base import MoveState
from rumil.orchestrators.common import has_adversarial_review
from rumil.tracing.tracer import CallTrace


def _claim(headline: str = "Test claim") -> Page:
    return Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        headline=headline,
        content=f"Content for {headline}",
        credence=6,
        robustness=3,
    )


@pytest_asyncio.fixture
async def target_claim(tmp_db):
    claim = _claim("Transformers will exceed 10T params by 2027.")
    await tmp_db.save_page(claim)
    return claim


@pytest_asyncio.fixture
async def adversarial_call(tmp_db, target_claim):
    call = Call(
        call_type=CallType.ADVERSARIAL_REVIEW,
        workspace=Workspace.RESEARCH,
        scope_page_id=target_claim.id,
        status=CallStatus.PENDING,
    )
    await tmp_db.save_call(call)
    return call


@pytest_asyncio.fixture
async def call_infra(tmp_db, target_claim, adversarial_call):
    return CallInfra(
        question_id=target_claim.id,
        call=adversarial_call,
        db=tmp_db,
        trace=CallTrace(adversarial_call.id, tmp_db),
        state=MoveState(adversarial_call, tmp_db),
    )


def _install_scout_stubs(mocker):
    def _make_fake_scout(label: str):
        class FakeScout:
            def __init__(self, question_id, call, db, **kwargs):
                self._question_id = question_id
                self._call = call
                self._db = db

            async def run(self):
                page = Page(
                    page_type=PageType.CLAIM,
                    layer=PageLayer.SQUIDGY,
                    workspace=Workspace.RESEARCH,
                    headline=f"[{label}] finding",
                    content=f"{label} content for {self._question_id[:8]}",
                    credence=5,
                    robustness=3,
                    provenance_call_id=self._call.id,
                    provenance_call_type=self._call.call_type.value,
                )
                await self._db.save_page(page)

        return FakeScout

    mocker.patch(
        "rumil.calls.adversarial_review.ScoutCHowTrueCall",
        _make_fake_scout("how_true"),
    )
    mocker.patch(
        "rumil.calls.adversarial_review.ScoutCHowFalseCall",
        _make_fake_scout("how_false"),
    )


async def _persist_verdict_for_target(
    db,
    target_id: str,
    verdict: AdversarialVerdict,
) -> Call:
    """Create a completed ADVERSARIAL_REVIEW call with a verdict page linked
    to *target_id*. Mirrors what the real updater persists; used by the
    has_adversarial_review tests."""
    call = Call(
        call_type=CallType.ADVERSARIAL_REVIEW,
        workspace=Workspace.RESEARCH,
        scope_page_id=target_id,
        status=CallStatus.COMPLETE,
    )
    await db.save_call(call)
    verdict_page = Page(
        page_type=PageType.JUDGEMENT,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        headline=f"Adversarial verdict: {target_id[:8]}",
        content=verdict.rationale,
        credence=verdict.confidence,
        robustness=3,
        provenance_call_id=call.id,
        provenance_call_type=call.call_type.value,
        extra={
            "adversarial_verdict": verdict.model_dump(mode="json"),
            "target_page_id": target_id,
        },
    )
    await db.save_page(verdict_page)
    await db.save_link(
        PageLink(
            from_page_id=verdict_page.id,
            to_page_id=target_id,
            link_type=LinkType.DEPENDS_ON,
            reasoning="Adversarial verdict.",
        )
    )
    return call


async def test_synthesizer_returns_verdict_with_concurrences_and_dissents(
    tmp_db, call_infra, target_claim, mocker
):
    """The updater must pass the structured verdict — including concurrences,
    dissents, and sunset_after_days — through to the persisted verdict page."""
    _install_scout_stubs(mocker)

    verdict = AdversarialVerdict(
        stronger_side="how_true",
        claim_holds=True,
        confidence=7,
        rationale=(
            "Scaling trends support the claim; the how-false critique did not "
            "engage with recent frontier runs."
        ),
        concurrences=[
            "Infrastructure spend alone implies >10T-param training runs are funded.",
            "MoE architectures make param counts cheaper than dense scaling.",
        ],
        dissents=[
            "Param count is a poor proxy for capability; the claim is trivially true but uninformative.",
            "Chinchilla-optimal scaling could keep frontier dense models well under 10T.",
        ],
        sunset_after_days=30,
    )

    async def fake_structured_call(*args, **kwargs):
        return StructuredCallResult(parsed=verdict, response_text="(stubbed)")

    mocker.patch(
        "rumil.calls.adversarial_review.structured_call",
        side_effect=fake_structured_call,
    )

    updater = AdversarialReviewUpdater()
    context = ContextResult(
        context_text=f"## Target `{target_claim.id[:8]}`",
        working_page_ids=[target_claim.id],
    )
    result = await updater.update_workspace(call_infra, context)

    assert len(result.created_page_ids) == 1
    verdict_page = await tmp_db.get_page(result.created_page_ids[0])
    assert verdict_page is not None
    stored = verdict_page.extra["adversarial_verdict"]
    assert stored["concurrences"] == verdict.concurrences
    assert stored["dissents"] == verdict.dissents
    assert stored["sunset_after_days"] == 30
    assert len(stored["concurrences"]) == 2
    assert len(stored["dissents"]) == 2


def test_is_verdict_expired_fresh_verdict_not_expired():
    """A verdict created just now with sunset=30 is not expired."""
    verdict = AdversarialVerdict(
        stronger_side="how_true",
        claim_holds=True,
        confidence=6,
        rationale="Fresh verdict.",
        sunset_after_days=30,
    )
    assert is_verdict_expired(verdict) is False


def test_is_verdict_expired_old_verdict_is_expired():
    """A verdict 31 days old with sunset=30 is expired."""
    verdict = AdversarialVerdict(
        stronger_side="how_true",
        claim_holds=True,
        confidence=6,
        rationale="Old verdict.",
        sunset_after_days=30,
        created_at=datetime.now(UTC) - timedelta(days=31),
    )
    assert is_verdict_expired(verdict) is True


def test_is_verdict_expired_null_sunset_never_expires():
    """A verdict with sunset_after_days=None never expires — structural claims."""
    verdict = AdversarialVerdict(
        stronger_side="how_true",
        claim_holds=True,
        confidence=8,
        rationale="Structural claim about logical necessity.",
        sunset_after_days=None,
        created_at=datetime.now(UTC) - timedelta(days=10_000),
    )
    assert is_verdict_expired(verdict) is False


async def test_has_adversarial_review_re_reviews_expired_verdict(tmp_db, target_claim):
    """has_adversarial_review returns True for a fresh verdict and False for an
    expired one — so the gate re-fires review when the sunset window lapses."""
    fresh_verdict = AdversarialVerdict(
        stronger_side="how_true",
        claim_holds=True,
        confidence=7,
        rationale="Fresh verdict that is still within its sunset window.",
        sunset_after_days=30,
    )
    await _persist_verdict_for_target(tmp_db, target_claim.id, fresh_verdict)
    assert await has_adversarial_review(tmp_db, target_claim.id) is True

    stale_claim = _claim("Stale claim")
    await tmp_db.save_page(stale_claim)
    expired_verdict = AdversarialVerdict(
        stronger_side="how_true",
        claim_holds=True,
        confidence=7,
        rationale="Verdict past its sunset window.",
        sunset_after_days=30,
        created_at=datetime.now(UTC) - timedelta(days=45),
    )
    await _persist_verdict_for_target(tmp_db, stale_claim.id, expired_verdict)
    assert await has_adversarial_review(tmp_db, stale_claim.id) is False
