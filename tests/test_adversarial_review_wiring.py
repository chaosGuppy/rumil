"""Tests for the wiring of AdversarialReviewCall into ClaimInvestigationOrchestrator.

Covers the gate that fires adversarial review on high-credence claims in the
claim-investigation loop, the settings flag, and the "already reviewed" check.
All tests mock the scouts and synthesizer so no API calls are made.
"""

import pytest
import pytest_asyncio

from rumil.calls.adversarial_review import AdversarialVerdict
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
from rumil.orchestrators.claim_investigation import ClaimInvestigationOrchestrator
from rumil.orchestrators.common import (
    adversarially_review_claim,
    has_adversarial_review,
)
from rumil.settings import override_settings


def _make_claim(headline: str = "Claim under test") -> Page:
    return Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        headline=headline,
        content=f"Content for {headline}",
        credence=5,
        robustness=3,
    )


def _make_judgement(question_id: str, credence: int) -> Page:
    return Page(
        page_type=PageType.JUDGEMENT,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        headline=f"Judgement on {question_id[:8]}",
        content="Judgement content.",
        credence=credence,
        robustness=3,
    )


async def _attach_judgement(db, claim: Page, credence: int) -> Page:
    judgement = _make_judgement(claim.id, credence)
    await db.save_page(judgement)
    await db.save_link(
        PageLink(
            from_page_id=judgement.id,
            to_page_id=claim.id,
            link_type=LinkType.ANSWERS,
            reasoning="Judgement answering the claim.",
        )
    )
    return judgement


@pytest_asyncio.fixture
async def claim_and_parent_call(tmp_db):
    claim = _make_claim()
    await tmp_db.save_page(claim)
    parent_call = Call(
        call_type=CallType.PRIORITIZATION,
        workspace=Workspace.PRIORITIZATION,
        scope_page_id=claim.id,
        status=CallStatus.RUNNING,
    )
    await tmp_db.save_call(parent_call)
    return claim, parent_call


def _stub_adversarial_run(mocker, verdict: AdversarialVerdict | None = None):
    """Patch AdversarialReviewCall.run to create a verdict JUDGEMENT and mark
    the call complete — bypasses both scouts and the synthesizer entirely.

    Returns a list that will collect target page IDs for each invocation, so
    tests can assert the review fired (and on what).
    """
    fired_on: list[str] = []

    async def fake_run(self):
        fired_on.append(self.infra.question_id)
        target_id = self.infra.question_id
        call = self.infra.call
        v = verdict or AdversarialVerdict(
            stronger_side="how_true",
            claim_holds=True,
            confidence=7,
            rationale="Stub rationale that satisfies the min-length constraint.",
        )
        verdict_page = Page(
            page_type=PageType.JUDGEMENT,
            layer=PageLayer.SQUIDGY,
            workspace=Workspace.RESEARCH,
            headline=f"Adversarial verdict: {target_id[:8]}",
            content=v.rationale,
            credence=v.confidence,
            robustness=3,
            provenance_call_id=call.id,
            provenance_call_type=call.call_type.value,
            extra={
                "adversarial_verdict": v.model_dump(),
                "target_page_id": target_id,
            },
        )
        await self.infra.db.save_page(verdict_page)
        await self.infra.db.save_link(
            PageLink(
                from_page_id=verdict_page.id,
                to_page_id=target_id,
                link_type=LinkType.DEPENDS_ON,
                reasoning="Adversarial review verdict.",
            )
        )
        call.status = CallStatus.COMPLETE
        await self.infra.db.save_call(call)

    mocker.patch(
        "rumil.calls.adversarial_review.AdversarialReviewCall.run",
        fake_run,
    )
    return fired_on


async def test_gate_fires_when_enabled_and_credence_crosses_threshold(
    tmp_db, claim_and_parent_call, mocker
):
    """With enable_adversarial_review=True, a claim whose latest judgement has
    credence >= 6 triggers an AdversarialReviewCall dispatched against it."""
    claim, parent_call = claim_and_parent_call
    await _attach_judgement(tmp_db, claim, credence=7)

    fired = _stub_adversarial_run(mocker)

    orch = ClaimInvestigationOrchestrator(tmp_db)
    orch._initial_call = parent_call

    with override_settings(rumil_test_mode="1", enable_adversarial_review=True):
        await orch._maybe_adversarial_review(claim.id)

    assert fired == [claim.id]
    assert await has_adversarial_review(tmp_db, claim.id)


async def test_gate_does_not_fire_when_disabled(tmp_db, claim_and_parent_call, mocker):
    """With enable_adversarial_review=False (default), no review fires even
    on high-credence claims."""
    claim, parent_call = claim_and_parent_call
    await _attach_judgement(tmp_db, claim, credence=8)

    fired = _stub_adversarial_run(mocker)

    orch = ClaimInvestigationOrchestrator(tmp_db)
    orch._initial_call = parent_call

    with override_settings(rumil_test_mode="1", enable_adversarial_review=False):
        await orch._maybe_adversarial_review(claim.id)

    assert fired == []
    assert not await has_adversarial_review(tmp_db, claim.id)


async def test_gate_skips_claim_already_reviewed(tmp_db, claim_and_parent_call, mocker):
    """If the claim has already been adversarially reviewed, a second review
    does NOT fire — one review per claim per run."""
    claim, parent_call = claim_and_parent_call
    await _attach_judgement(tmp_db, claim, credence=7)

    fired = _stub_adversarial_run(mocker)

    orch = ClaimInvestigationOrchestrator(tmp_db)
    orch._initial_call = parent_call

    with override_settings(rumil_test_mode="1", enable_adversarial_review=True):
        await orch._maybe_adversarial_review(claim.id)
        assert fired == [claim.id]
        await orch._maybe_adversarial_review(claim.id)

    assert fired == [claim.id], "Second call should not re-fire the review"


async def test_verdict_that_denies_claim_lands_visible_signal(
    tmp_db, claim_and_parent_call, mocker
):
    """When the verdict says the claim does NOT hold, the verdict is persisted
    on a JUDGEMENT page linked to the claim via DEPENDS_ON, with the structured
    verdict stored in extra — a signal downstream code can detect."""
    claim, parent_call = claim_and_parent_call
    await _attach_judgement(tmp_db, claim, credence=7)

    denying_verdict = AdversarialVerdict(
        stronger_side="how_false",
        claim_holds=False,
        confidence=3,
        rationale=(
            "The how-false side surfaced a direct defeater the how-true side "
            "did not engage with. The claim does not hold at current evidence."
        ),
    )
    _stub_adversarial_run(mocker, verdict=denying_verdict)

    verdict = await adversarially_review_claim(
        tmp_db,
        parent_call,
        claim.id,
    )
    assert verdict is not None
    assert verdict.claim_holds is False
    assert verdict.stronger_side == "how_false"

    links_to_claim = await tmp_db.get_links_to(claim.id)
    verdict_links = [l for l in links_to_claim if l.link_type == LinkType.DEPENDS_ON]
    assert len(verdict_links) == 1
    verdict_page = await tmp_db.get_page(verdict_links[0].from_page_id)
    assert verdict_page is not None
    assert verdict_page.page_type == PageType.JUDGEMENT
    stored = verdict_page.extra["adversarial_verdict"]
    assert stored["claim_holds"] is False
    assert stored["stronger_side"] == "how_false"
    assert verdict_page.extra["target_page_id"] == claim.id


async def test_gate_skips_when_credence_below_threshold(tmp_db, claim_and_parent_call, mocker):
    """Claims whose latest judgement has credence < threshold do not fire."""
    claim, parent_call = claim_and_parent_call
    await _attach_judgement(tmp_db, claim, credence=5)

    fired = _stub_adversarial_run(mocker)

    orch = ClaimInvestigationOrchestrator(tmp_db)
    orch._initial_call = parent_call

    with override_settings(rumil_test_mode="1", enable_adversarial_review=True):
        await orch._maybe_adversarial_review(claim.id)

    assert fired == []


async def test_available_moves_preset_has_adversarial_review_entry():
    """Every preset in available_moves.PRESETS must have an ADVERSARIAL_REVIEW
    entry (even if empty), otherwise get_moves_for_call raises ValueError."""
    from rumil.available_moves import PRESETS, get_moves_for_call

    for preset_name in PRESETS:
        with override_settings(rumil_test_mode="1", available_moves=preset_name):
            moves = get_moves_for_call(CallType.ADVERSARIAL_REVIEW)
            assert moves == [] or moves == ()


@pytest.mark.parametrize(
    ("credence", "expected_fired"),
    [
        (6, True),
        (9, True),
        (5, False),
        (1, False),
    ],
)
async def test_threshold_boundary(credence, expected_fired, tmp_db, claim_and_parent_call, mocker):
    """Threshold is inclusive: credence == 6 fires, credence == 5 does not."""
    claim, parent_call = claim_and_parent_call
    await _attach_judgement(tmp_db, claim, credence=credence)

    fired = _stub_adversarial_run(mocker)

    orch = ClaimInvestigationOrchestrator(tmp_db)
    orch._initial_call = parent_call

    with override_settings(rumil_test_mode="1", enable_adversarial_review=True):
        await orch._maybe_adversarial_review(claim.id)

    assert bool(fired) is expected_fired
