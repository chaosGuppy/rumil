"""Tests for the post-assess adversarial-review gate in ``assess_question``.

Covers the hoisted gate that fires adversarial review for any orchestrator
that routes through ``assess_question`` (source_first, distill_first,
critique_first, worldview, two_phase, claim_investigation). All tests mock
the assess call to avoid any real LLM traffic.
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
from rumil.orchestrators.common import assess_question, has_adversarial_review
from rumil.settings import override_settings


def _make_claim(headline: str = "Gate-under-test claim") -> Page:
    return Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        headline=headline,
        content=f"Content for {headline}",
        credence=5,
        robustness=3,
    )


def _make_judgement_for(target_id: str, credence: int) -> Page:
    return Page(
        page_type=PageType.JUDGEMENT,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        headline=f"Judgement on {target_id[:8]}",
        content="Judgement content.",
        credence=credence,
        robustness=3,
    )


@pytest_asyncio.fixture
async def target_claim(tmp_db):
    claim = _make_claim()
    await tmp_db.save_page(claim)
    return claim


def _stub_assess(mocker, credence: int):
    """Replace the default assess runner with a fake that writes a judgement
    page of the given credence linked to the assessed page. No LLM traffic.
    """

    class FakeAssessCall:
        def __init__(self, question_id, call, db, **kwargs):
            self._question_id = question_id
            self._call = call
            self._db = db

        async def run(self):
            j = _make_judgement_for(self._question_id, credence)
            j.provenance_call_id = self._call.id
            j.provenance_call_type = self._call.call_type.value
            await self._db.save_page(j)
            await self._db.save_link(
                PageLink(
                    from_page_id=j.id,
                    to_page_id=self._question_id,
                    link_type=LinkType.ANSWERS,
                    reasoning="Fake assess judgement for gate test.",
                )
            )
            self._call.status = CallStatus.COMPLETE
            await self._db.save_call(self._call)

    mocker.patch.dict(
        "rumil.orchestrators.common.ASSESS_CALL_CLASSES",
        {"default": FakeAssessCall},
    )


def _stub_adversarial_run(mocker, verdict: AdversarialVerdict | None = None):
    """Replace ``AdversarialReviewCall.run`` with a fake that writes a verdict
    JUDGEMENT and completes the call. Returns a list that captures the target
    page ID of every invocation."""
    fired_on: list[str] = []

    async def fake_run(self):
        fired_on.append(self.infra.question_id)
        target_id = self.infra.question_id
        call = self.infra.call
        v = verdict or AdversarialVerdict(
            stronger_side="how_true",
            claim_holds=True,
            claim_confidence=7,
            rationale="Stub verdict satisfying min-length for test purposes.",
        )
        verdict_page = Page(
            page_type=PageType.JUDGEMENT,
            layer=PageLayer.SQUIDGY,
            workspace=Workspace.RESEARCH,
            headline=f"Adversarial verdict: {target_id[:8]}",
            content=v.rationale,
            credence=v.claim_confidence,
            robustness=3,
            provenance_call_id=call.id,
            provenance_call_type=call.call_type.value,
            extra={
                "adversarial_verdict": v.model_dump(mode="json"),
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


async def test_gate_fires_after_assess_when_enabled_and_credence_crosses_threshold(
    tmp_db, target_claim, mocker
):
    """assess_question fires adversarial review when the post-assess credence
    on the assessed page meets the threshold. This gives every orchestrator the
    gate for free — the whole point of hoisting it out of claim_investigation.
    """
    _stub_assess(mocker, credence=7)
    fired = _stub_adversarial_run(mocker)

    with override_settings(rumil_test_mode="1", enable_adversarial_review=True):
        await assess_question(target_claim.id, tmp_db)

    assert fired == [target_claim.id]
    assert await has_adversarial_review(tmp_db, target_claim.id)


async def test_gate_does_not_fire_when_disabled(tmp_db, target_claim, mocker):
    """With enable_adversarial_review=False (default), assess_question does not
    trigger review even when credence crosses the threshold."""
    _stub_assess(mocker, credence=8)
    fired = _stub_adversarial_run(mocker)

    with override_settings(rumil_test_mode="1", enable_adversarial_review=False):
        await assess_question(target_claim.id, tmp_db)

    assert fired == []
    assert not await has_adversarial_review(tmp_db, target_claim.id)


async def test_gate_skips_page_already_reviewed(tmp_db, target_claim, mocker):
    """If the assessed page already has a valid adversarial verdict, the gate
    does not re-fire the review (one-per-target-per-run semantics)."""
    prior_call = Call(
        call_type=CallType.ADVERSARIAL_REVIEW,
        workspace=Workspace.RESEARCH,
        scope_page_id=target_claim.id,
        status=CallStatus.COMPLETE,
    )
    await tmp_db.save_call(prior_call)
    prior_verdict = AdversarialVerdict(
        stronger_side="how_true",
        claim_holds=True,
        claim_confidence=6,
        rationale="Prior verdict already on file; gate must respect it.",
    )
    prior_verdict_page = Page(
        page_type=PageType.JUDGEMENT,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        headline=f"Prior verdict: {target_claim.id[:8]}",
        content=prior_verdict.rationale,
        credence=prior_verdict.claim_confidence,
        robustness=3,
        provenance_call_id=prior_call.id,
        provenance_call_type=prior_call.call_type.value,
        extra={
            "adversarial_verdict": prior_verdict.model_dump(mode="json"),
            "target_page_id": target_claim.id,
        },
    )
    await tmp_db.save_page(prior_verdict_page)
    await tmp_db.save_link(
        PageLink(
            from_page_id=prior_verdict_page.id,
            to_page_id=target_claim.id,
            link_type=LinkType.DEPENDS_ON,
            reasoning="Prior adversarial verdict.",
        )
    )

    _stub_assess(mocker, credence=9)
    fired = _stub_adversarial_run(mocker)

    with override_settings(rumil_test_mode="1", enable_adversarial_review=True):
        await assess_question(target_claim.id, tmp_db)

    assert fired == [], "Second review should not fire when one already exists"


async def test_gate_skips_when_credence_below_threshold(tmp_db, target_claim, mocker):
    """When assess results in credence < threshold, the gate stays silent."""
    _stub_assess(mocker, credence=5)
    fired = _stub_adversarial_run(mocker)

    with override_settings(rumil_test_mode="1", enable_adversarial_review=True):
        await assess_question(target_claim.id, tmp_db)

    assert fired == []
    assert not await has_adversarial_review(tmp_db, target_claim.id)


async def test_gate_failure_does_not_propagate_to_assess_caller(tmp_db, target_claim, mocker):
    """If the adversarial review raises, assess_question must still return its
    assess call ID — review failures are logged and swallowed."""
    _stub_assess(mocker, credence=8)

    async def boom(self):
        raise RuntimeError("simulated adversarial failure")

    mocker.patch(
        "rumil.calls.adversarial_review.AdversarialReviewCall.run",
        boom,
    )

    with override_settings(rumil_test_mode="1", enable_adversarial_review=True):
        returned = await assess_question(target_claim.id, tmp_db)

    assert returned is not None, "assess_question should still return the assess call ID"
