"""Tests for the adversarial-review wrapper (src/rumil/calls/adversarial_review.py).

These tests use `tmp_db` for real Supabase I/O but stub both scout dispatches
and the synthesizer LLM so no API calls are made. They verify that the updater
coordinates the two scouts, feeds their output to a synthesizer, and persists
a verdict page linked to the target with DEPENDS_ON.
"""

import pytest
import pytest_asyncio

from rumil.calls.adversarial_review import (
    SYNTHESIZER_PROMPT_FILE,
    AdversarialReviewCall,
    AdversarialReviewUpdater,
    AdversarialVerdict,
)
from rumil.calls.stages import CallInfra, ContextResult
from rumil.llm import StructuredCallResult, _load_file
from rumil.models import (
    Call,
    CallStatus,
    CallType,
    LinkType,
    Page,
    PageLayer,
    PageType,
    Workspace,
)
from rumil.moves.base import MoveState
from rumil.tracing.tracer import CallTrace


def _claim(headline: str, content: str = "") -> Page:
    return Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        headline=headline,
        content=content or f"Content for {headline}",
        credence=6,
        robustness=3,
    )


@pytest_asyncio.fixture
async def target_claim(tmp_db):
    claim = _claim(
        "Frontier AI will automate most routine cognitive labour by 2030.",
    )
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


def _canned_verdict() -> AdversarialVerdict:
    return AdversarialVerdict(
        stronger_side="how_false",
        claim_holds=False,
        confidence=3,
        rationale=(
            "The how-false scout surfaced a direct defeater — the current "
            "deployment trajectory shows the bottleneck is integration, not "
            "raw capability — that the how-true side did not engage with. "
            "The how-true arguments leaned on aggregate capability trends "
            "without mechanism-level detail. Two unresolved cruxes remain: "
            "long-horizon agency reliability and institutional adoption rates. "
            "Low confidence reflects that the scouts did not cite recent "
            "deployment data."
        ),
    )


def _install_scout_stubs(mocker, tmp_db_factory_page_ids):
    """Replace the two scout CallRunner classes with fake ones that write a
    page to the DB during their run() so _render_scout_output has content.

    tmp_db_factory_page_ids is a dict {"how_true": [...], "how_false": [...]}
    populated by the stub runs so tests can assert on what the scouts wrote.
    """

    def _make_fake_scout(scout_label: str, page_type: PageType):
        class FakeScout:
            def __init__(self, question_id, call, db, **kwargs):
                self._question_id = question_id
                self._call = call
                self._db = db

            async def run(self):
                page = Page(
                    page_type=page_type,
                    layer=PageLayer.SQUIDGY,
                    workspace=Workspace.RESEARCH,
                    headline=f"[{scout_label}] Canned scout output",
                    content=f"Canned {scout_label} finding about {self._question_id[:8]}.",
                    credence=5,
                    robustness=3,
                    provenance_call_id=self._call.id,
                    provenance_call_type=self._call.call_type.value,
                )
                await self._db.save_page(page)
                tmp_db_factory_page_ids.setdefault(scout_label, []).append(page.id)

        return FakeScout

    mocker.patch(
        "rumil.calls.adversarial_review.ScoutCHowTrueCall",
        _make_fake_scout("how_true", PageType.CLAIM),
    )
    mocker.patch(
        "rumil.calls.adversarial_review.ScoutCHowFalseCall",
        _make_fake_scout("how_false", PageType.CLAIM),
    )


def _install_synth_stub(mocker, verdict: AdversarialVerdict):
    captured: dict = {}

    async def fake_structured_call(*args, **kwargs):
        captured["system_prompt"] = kwargs.get("system_prompt") or (args[0] if args else None)
        captured["user_message"] = kwargs.get("user_message") or (
            args[1] if len(args) > 1 else None
        )
        captured["response_model"] = kwargs.get("response_model")
        return StructuredCallResult(parsed=verdict, response_text="(stubbed)")

    mocker.patch(
        "rumil.calls.adversarial_review.structured_call",
        side_effect=fake_structured_call,
    )
    return captured


async def test_synthesizer_prompt_is_loadable():
    """The synthesizer prompt file must exist and be non-empty."""
    text = _load_file(SYNTHESIZER_PROMPT_FILE)
    assert text.strip()
    assert "stronger_side" in text
    assert "claim_holds" in text
    assert "confidence" in text
    assert "rationale" in text


def test_synthesizer_prompt_constant_matches_file():
    """Safety net: the constant used by the updater must name the real file."""
    assert SYNTHESIZER_PROMPT_FILE.endswith(".md")
    assert _load_file(SYNTHESIZER_PROMPT_FILE)


def test_adversarial_verdict_validates_fields():
    """Schema sanity check: the verdict model enforces the documented fields."""
    v = AdversarialVerdict(
        stronger_side="how_true",
        claim_holds=True,
        confidence=7,
        rationale="Adequate rationale.",
    )
    assert v.stronger_side == "how_true"
    assert v.claim_holds is True
    assert v.confidence == 7

    with pytest.raises(Exception):
        AdversarialVerdict(
            stronger_side="how_true",
            claim_holds=True,
            confidence=10,
            rationale="Out of range confidence.",
        )


def test_adversarial_review_is_exported():
    """The wrapper must be importable from rumil.calls."""
    from rumil.calls import AdversarialReviewCall as Imported
    from rumil.calls import AdversarialVerdict as ImportedVerdict

    assert Imported is AdversarialReviewCall
    assert ImportedVerdict is AdversarialVerdict


def test_call_type_registered():
    """CallType.ADVERSARIAL_REVIEW is defined on the enum."""
    assert CallType.ADVERSARIAL_REVIEW.value == "adversarial_review"
    assert CallType.ADVERSARIAL_REVIEW not in _dispatchable_call_types()


def _dispatchable_call_types() -> set[CallType]:
    from rumil.models import DISPATCHABLE_CALL_TYPES

    return DISPATCHABLE_CALL_TYPES


async def test_updater_records_verdict_page_and_link(tmp_db, call_infra, target_claim, mocker):
    """End-to-end: the updater runs both scouts, synthesizes, and persists
    a JUDGEMENT verdict page linked to the target by DEPENDS_ON."""
    scout_pages: dict = {}
    _install_scout_stubs(mocker, scout_pages)
    verdict = _canned_verdict()
    captured = _install_synth_stub(mocker, verdict)

    updater = AdversarialReviewUpdater()
    context = ContextResult(
        context_text=f"## Target `{target_claim.id[:8]}`\n\n{target_claim.content}",
        working_page_ids=[target_claim.id],
    )

    result = await updater.update_workspace(call_infra, context)

    assert len(result.created_page_ids) == 1
    verdict_id = result.created_page_ids[0]
    verdict_page = await tmp_db.get_page(verdict_id)
    assert verdict_page is not None
    assert verdict_page.page_type == PageType.JUDGEMENT
    assert verdict_page.credence == verdict.confidence
    assert verdict_page.provenance_call_id == call_infra.call.id
    assert verdict_page.extra["target_page_id"] == target_claim.id
    stored = verdict_page.extra["adversarial_verdict"]
    assert stored["stronger_side"] == verdict.stronger_side
    assert stored["claim_holds"] == verdict.claim_holds
    assert stored["confidence"] == verdict.confidence
    assert "does not hold" in verdict_page.headline
    assert verdict.rationale in verdict_page.content

    links = await tmp_db.get_links_from(verdict_id)
    depends_on = [link for link in links if link.link_type == LinkType.DEPENDS_ON]
    assert len(depends_on) == 1
    assert depends_on[0].to_page_id == target_claim.id

    assert "how_true" in scout_pages and len(scout_pages["how_true"]) == 1
    assert "how_false" in scout_pages and len(scout_pages["how_false"]) == 1

    assert captured["response_model"] is AdversarialVerdict
    assert "How-True Scout Output" in captured["user_message"]
    assert "How-False Scout Output" in captured["user_message"]
    assert scout_pages["how_true"][0][:8] in captured["user_message"]
    assert scout_pages["how_false"][0][:8] in captured["user_message"]
    assert captured["system_prompt"].startswith("# Adversarial Review")


async def test_updater_dispatches_both_scouts_with_correct_call_types(
    tmp_db, call_infra, target_claim, mocker
):
    """The updater must create one SCOUT_C_HOW_TRUE call and one SCOUT_C_HOW_FALSE
    call, both parented to the adversarial-review call."""
    scout_pages: dict = {}
    _install_scout_stubs(mocker, scout_pages)
    _install_synth_stub(mocker, _canned_verdict())

    updater = AdversarialReviewUpdater()
    context = ContextResult(context_text="stub", working_page_ids=[target_claim.id])
    await updater.update_workspace(call_infra, context)

    rows = (
        await tmp_db.client.table("calls")
        .select("id, call_type, parent_call_id, scope_page_id")
        .eq("run_id", tmp_db.run_id)
        .execute()
    )
    child_calls = [r for r in rows.data if r.get("parent_call_id") == call_infra.call.id]
    types = {r["call_type"] for r in child_calls}
    assert types == {
        CallType.SCOUT_C_HOW_TRUE.value,
        CallType.SCOUT_C_HOW_FALSE.value,
    }
    for r in child_calls:
        assert r["scope_page_id"] == target_claim.id


async def test_updater_raises_when_synthesizer_returns_no_parsed_output(
    tmp_db, call_infra, target_claim, mocker
):
    """If the synthesizer LLM fails to produce a parseable verdict, we raise
    rather than silently skip — verdict creation is the point of the call."""
    scout_pages: dict = {}
    _install_scout_stubs(mocker, scout_pages)

    async def fake_structured_call(*args, **kwargs):
        return StructuredCallResult(parsed=None, response_text="(invalid)")

    mocker.patch(
        "rumil.calls.adversarial_review.structured_call",
        side_effect=fake_structured_call,
    )

    updater = AdversarialReviewUpdater()
    context = ContextResult(context_text="stub", working_page_ids=[target_claim.id])

    with pytest.raises(ValueError, match="no parseable verdict"):
        await updater.update_workspace(call_infra, context)


async def test_adversarial_review_call_attaches_correct_stages(
    tmp_db, target_claim, adversarial_call
):
    """Smoke test: instantiating the CallRunner wires the stage classes we expect."""
    runner = AdversarialReviewCall(
        target_claim.id,
        adversarial_call,
        tmp_db,
    )
    assert runner.call_type == CallType.ADVERSARIAL_REVIEW
    assert runner.context_builder.__class__.__name__ == "AdversarialReviewContext"
    assert runner.workspace_updater.__class__.__name__ == "AdversarialReviewUpdater"
    assert runner.closing_reviewer.__class__.__name__ == "StandardClosingReview"
    task = runner.task_description()
    assert target_claim.id in task
