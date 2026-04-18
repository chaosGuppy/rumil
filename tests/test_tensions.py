"""Tests for the tensions tracker: detection primitive, ExploreTensionCall,
and TensionExplorationPolicy.

Per task spec: zero real LLM calls. Mocks are applied at the highest
boundary (structured_call + scout CallRunner classes) so the tests
exercise the real orchestration and persistence paths while isolating
the network boundary.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from rumil.calls.explore_tension import (
    EXPLORE_TENSION_PROMPT_FILE,
    ExploreTensionCall,
    ExploreTensionUpdater,
    TensionVerdict,
)
from rumil.calls.stages import CallInfra, ContextResult
from rumil.llm import StructuredCallResult, _load_file
from rumil.models import (
    DISPATCHABLE_CALL_TYPES,
    Call,
    CallStatus,
    CallType,
    ConsiderationDirection,
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Suggestion,
    SuggestionStatus,
    SuggestionType,
    Workspace,
)
from rumil.moves.base import MoveState
from rumil.orchestrators.policy_layer import (
    DispatchCall,
    QuestionState,
    TensionExplorationPolicy,
)
from rumil.tensions import (
    TENSION_CREDENCE_THRESHOLD,
    TensionCandidate,
    find_tension_candidates,
    unexplored_tension_candidates,
)
from rumil.tracing.tracer import CallTrace


def _question(headline: str = "Will X happen?") -> Page:
    return Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        headline=headline,
        content=headline,
    )


def _claim(headline: str, *, credence: int | None) -> Page:
    return Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        headline=headline,
        content=f"Content for {headline}",
        credence=credence,
        robustness=3,
    )


def _link(
    claim_id: str,
    question_id: str,
    direction: ConsiderationDirection | None,
) -> PageLink:
    return PageLink(
        from_page_id=claim_id,
        to_page_id=question_id,
        link_type=LinkType.CONSIDERATION,
        direction=direction,
    )


def _mock_db(
    *,
    question: Page | None,
    considerations: list[tuple[Page, PageLink]],
    pending_suggestions: list[Suggestion] | None = None,
    verdict_pages: list[Page] | None = None,
) -> MagicMock:
    db = MagicMock()
    db.run_id = str(uuid.uuid4())
    db.project_id = str(uuid.uuid4())
    db.staged = False

    async def _get_page(page_id: str) -> Page | None:
        if question is not None and page_id == question.id:
            return question
        return None

    db.get_page = AsyncMock(side_effect=_get_page)
    db.get_considerations_for_question = AsyncMock(return_value=considerations)
    db.get_pending_suggestions = AsyncMock(return_value=pending_suggestions or [])
    db.get_tension_verdicts_for_question = AsyncMock(return_value=verdict_pages or [])
    db.save_suggestion = AsyncMock()
    return db


async def test_direction_conflict_surfaces_exactly_one_candidate():
    """Two high-credence claims with SUPPORTS vs OPPOSES directions on the
    same question yield exactly one direction_conflict candidate, even when
    a third claim is present but doesn't conflict."""
    q = _question()
    pro = _claim("Strong pro claim", credence=7)
    con = _claim("Strong con claim", credence=7)
    neutral = _claim("Unrelated neutral claim", credence=6)
    considerations = [
        (pro, _link(pro.id, q.id, ConsiderationDirection.SUPPORTS)),
        (con, _link(con.id, q.id, ConsiderationDirection.OPPOSES)),
        (neutral, _link(neutral.id, q.id, ConsiderationDirection.NEUTRAL)),
    ]
    db = _mock_db(question=q, considerations=considerations)

    candidates = await find_tension_candidates(db, q.id)

    assert len(candidates) == 1
    c = candidates[0]
    assert c.kind == "direction_conflict"
    assert c.question_id == q.id
    assert {c.claim_a_id, c.claim_b_id} == {pro.id, con.id}
    assert c.claim_a_id < c.claim_b_id
    assert c.confidence == 1.0


async def test_direction_conflict_ignores_low_credence_claims():
    """Claims below the credence threshold do not produce candidates even
    when their directions conflict."""
    q = _question()
    low_pro = _claim("Weak pro claim", credence=3)
    high_con = _claim("Strong con claim", credence=7)
    considerations = [
        (low_pro, _link(low_pro.id, q.id, ConsiderationDirection.SUPPORTS)),
        (high_con, _link(high_con.id, q.id, ConsiderationDirection.OPPOSES)),
    ]
    db = _mock_db(question=q, considerations=considerations)

    candidates = await find_tension_candidates(db, q.id)

    assert candidates == []


async def test_neutral_and_same_direction_do_not_conflict():
    """NEUTRAL + SUPPORTS, or two SUPPORTS, do not trigger detection."""
    q = _question()
    pro_a = _claim("Pro A", credence=8)
    pro_b = _claim("Pro B", credence=8)
    neutral = _claim("Neutral", credence=7)
    considerations = [
        (pro_a, _link(pro_a.id, q.id, ConsiderationDirection.SUPPORTS)),
        (pro_b, _link(pro_b.id, q.id, ConsiderationDirection.SUPPORTS)),
        (neutral, _link(neutral.id, q.id, ConsiderationDirection.NEUTRAL)),
    ]
    db = _mock_db(question=q, considerations=considerations)

    assert await find_tension_candidates(db, q.id) == []


async def test_semantic_detection_fires_llm_and_returns_candidate(mocker):
    """When include_semantic=True and the LLM flags a pair (that the
    structural scan did NOT), the result is captured as a
    semantic_contradiction TensionCandidate."""
    q = _question("Does X increase risk?")
    same_direction_a = _claim("A: X increases risk via mechanism M1", credence=7)
    same_direction_b = _claim("B: X reduces risk via mechanism M2", credence=7)
    considerations = [
        (same_direction_a, _link(same_direction_a.id, q.id, ConsiderationDirection.SUPPORTS)),
        (same_direction_b, _link(same_direction_b.id, q.id, ConsiderationDirection.SUPPORTS)),
    ]
    db = _mock_db(question=q, considerations=considerations)

    captured: dict = {}

    async def fake_structured_call(*args, **kwargs):
        captured["system_prompt"] = kwargs.get("system_prompt") or (args[0] if args else None)
        captured["user_message"] = kwargs.get("user_message")
        response_model = kwargs.get("response_model")
        assert response_model is not None
        captured["response_model"] = response_model
        verdict = response_model(
            in_tension=True,
            reason="The two mechanisms point in opposite directions.",
            confidence=0.82,
            kind="semantic_contradiction",
        )
        return StructuredCallResult(parsed=verdict, response_text="(stub)")

    mocker.patch("rumil.tensions.structured_call", side_effect=fake_structured_call)

    candidates = await find_tension_candidates(
        db, q.id, include_semantic=True, max_semantic_pairs=5
    )

    assert len(candidates) == 1
    cand = candidates[0]
    assert cand.kind == "semantic_contradiction"
    assert {cand.claim_a_id, cand.claim_b_id} == {
        same_direction_a.id,
        same_direction_b.id,
    }
    assert cand.reason == "The two mechanisms point in opposite directions."
    assert cand.confidence == pytest.approx(0.82)
    assert q.headline in captured["user_message"]
    assert same_direction_a.headline in captured["user_message"]
    assert same_direction_b.headline in captured["user_message"]


async def test_semantic_detection_skips_pairs_already_flagged_structurally(mocker):
    """If structural scan already flagged a pair, the semantic pass should
    not re-query the LLM on it — one candidate, not two, per pair."""
    q = _question()
    pro = _claim("pro", credence=7)
    con = _claim("con", credence=7)
    considerations = [
        (pro, _link(pro.id, q.id, ConsiderationDirection.SUPPORTS)),
        (con, _link(con.id, q.id, ConsiderationDirection.OPPOSES)),
    ]
    db = _mock_db(question=q, considerations=considerations)
    mock_call = AsyncMock()
    mocker.patch("rumil.tensions.structured_call", side_effect=mock_call)

    candidates = await find_tension_candidates(db, q.id, include_semantic=True)

    assert len(candidates) == 1
    assert candidates[0].kind == "direction_conflict"
    mock_call.assert_not_called()


async def test_semantic_detection_does_not_fire_when_include_semantic_false(mocker):
    """include_semantic=False must skip the LLM entirely — even on pairs
    the structural scan didn't flag."""
    q = _question()
    a = _claim("A", credence=7)
    b = _claim("B", credence=7)
    considerations = [
        (a, _link(a.id, q.id, ConsiderationDirection.SUPPORTS)),
        (b, _link(b.id, q.id, ConsiderationDirection.SUPPORTS)),
    ]
    db = _mock_db(question=q, considerations=considerations)
    mock_call = AsyncMock()
    mocker.patch("rumil.tensions.structured_call", side_effect=mock_call)

    assert await find_tension_candidates(db, q.id, include_semantic=False) == []
    mock_call.assert_not_called()


async def test_tension_candidate_make_canonicalises_pair_ordering():
    """Passing the same pair in either argument order yields the same
    canonical representation — needed for deduplication across passes."""
    id_x = "aaaa" + "0" * 32
    id_y = "bbbb" + "0" * 32
    a = TensionCandidate.make(
        "q1", id_x, id_y, kind="direction_conflict", reason="", confidence=1.0
    )
    b = TensionCandidate.make(
        "q1", id_y, id_x, kind="direction_conflict", reason="", confidence=1.0
    )
    assert a.pair_key == b.pair_key
    assert a.claim_a_id == b.claim_a_id == id_x


async def test_unexplored_filters_out_pairs_with_existing_suggestion():
    """unexplored_tension_candidates should drop candidates whose pair is
    already represented in a pending RESOLVE_TENSION suggestion."""
    q = _question()
    pro = _claim("pro", credence=7)
    con = _claim("con", credence=7)
    considerations = [
        (pro, _link(pro.id, q.id, ConsiderationDirection.SUPPORTS)),
        (con, _link(con.id, q.id, ConsiderationDirection.OPPOSES)),
    ]
    a, b = sorted([pro.id, con.id])
    existing = Suggestion(
        project_id="proj",
        workspace="research",
        run_id="run",
        suggestion_type=SuggestionType.RESOLVE_TENSION,
        target_page_id=a,
        source_page_id=b,
        payload={"question_id": q.id, "claim_a_id": a, "claim_b_id": b},
        status=SuggestionStatus.PENDING,
    )
    db = _mock_db(question=q, considerations=considerations, pending_suggestions=[existing])

    assert await unexplored_tension_candidates(db, q.id) == []


def _canned_tension_verdict(resolution: str = "a_survives") -> TensionVerdict:
    return TensionVerdict(
        resolution=resolution,  # type: ignore[arg-type]
        rationale=(
            "The how-true scout on claim A surfaced a direct mechanism with "
            "concrete observable consequences, while the how-false scout on "
            "claim B leaned on aggregate trends without mechanism-level "
            "detail. Claim A's case is crisper and better grounded. Claim B "
            "still identifies a real consideration — institutional adoption "
            "lag — which is preserved in the dissents."
        ),
        refining_claim_headline=None,
        refining_claim_content=None,
        confidence=6,
    )


def _refining_verdict() -> TensionVerdict:
    return TensionVerdict(
        resolution="both_survive_refined",
        rationale="Both claims hold once the short-term / long-term distinction is explicit.",
        refining_claim_headline="Short-term vs long-term split resolves the tension",
        refining_claim_content="In the short term A dominates; in the long term B dominates.",
        confidence=6,
    )


@pytest_asyncio.fixture
async def tension_triple(tmp_db):
    q = _question("Will frontier AI automate routine cognitive labour by 2030?")
    claim_a = _claim("Yes, deployment trajectory supports it", credence=7)
    claim_b = _claim("No, integration bottlenecks dominate", credence=7)
    await tmp_db.save_page(q)
    await tmp_db.save_page(claim_a)
    await tmp_db.save_page(claim_b)
    link_a = PageLink(
        from_page_id=claim_a.id,
        to_page_id=q.id,
        link_type=LinkType.CONSIDERATION,
        direction=ConsiderationDirection.SUPPORTS,
    )
    link_b = PageLink(
        from_page_id=claim_b.id,
        to_page_id=q.id,
        link_type=LinkType.CONSIDERATION,
        direction=ConsiderationDirection.OPPOSES,
    )
    await tmp_db.save_link(link_a)
    await tmp_db.save_link(link_b)
    return q, claim_a, claim_b


@pytest_asyncio.fixture
async def explore_tension_call(tmp_db, tension_triple):
    q, claim_a, claim_b = tension_triple
    call = Call(
        call_type=CallType.EXPLORE_TENSION,
        workspace=Workspace.RESEARCH,
        scope_page_id=q.id,
        status=CallStatus.PENDING,
        call_params={
            "tension_question_id": q.id,
            "tension_claim_a_id": claim_a.id,
            "tension_claim_b_id": claim_b.id,
            "tension_kind": "direction_conflict",
            "tension_reason": "Directions conflict.",
        },
    )
    await tmp_db.save_call(call)
    return call


@pytest_asyncio.fixture
async def explore_infra(tmp_db, tension_triple, explore_tension_call):
    q, _, _ = tension_triple
    return CallInfra(
        question_id=q.id,
        call=explore_tension_call,
        db=tmp_db,
        trace=CallTrace(explore_tension_call.id, tmp_db),
        state=MoveState(explore_tension_call, tmp_db),
    )


def _install_scout_stubs(mocker, scout_pages: dict):
    """Replace the two scout CallRunner classes with fakes that write a
    page during run() so _render_scout_output finds content."""

    def _make_fake(label: str):
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
                    headline=f"[{label}] canned output",
                    content=f"Canned {label} finding about {self._question_id[:8]}.",
                    credence=5,
                    robustness=3,
                    provenance_call_id=self._call.id,
                    provenance_call_type=self._call.call_type.value,
                )
                await self._db.save_page(page)
                scout_pages.setdefault(label, []).append(page.id)

        return FakeScout

    mocker.patch(
        "rumil.calls.explore_tension.ScoutCHowTrueCall",
        _make_fake("how_true"),
    )
    mocker.patch(
        "rumil.calls.explore_tension.ScoutCHowFalseCall",
        _make_fake("how_false"),
    )


def _install_synth_stub(mocker, verdict: TensionVerdict):
    captured: dict = {}

    async def fake_structured_call(*args, **kwargs):
        captured["system_prompt"] = kwargs.get("system_prompt") or (args[0] if args else None)
        captured["user_message"] = kwargs.get("user_message")
        captured["response_model"] = kwargs.get("response_model")
        return StructuredCallResult(parsed=verdict, response_text="(stub)")

    mocker.patch(
        "rumil.calls.explore_tension.structured_call",
        side_effect=fake_structured_call,
    )
    return captured


def test_explore_tension_prompt_loadable():
    text = _load_file(EXPLORE_TENSION_PROMPT_FILE)
    assert text.strip()
    assert "resolution" in text
    assert "rationale" in text
    assert "confidence" in text


def test_explore_tension_call_type_registered():
    assert CallType.EXPLORE_TENSION.value == "explore_tension"
    assert CallType.EXPLORE_TENSION not in DISPATCHABLE_CALL_TYPES


def test_explore_tension_is_exported():
    from rumil.calls import ExploreTensionCall as Imported
    from rumil.calls import TensionVerdict as ImportedVerdict

    assert Imported is ExploreTensionCall
    assert ImportedVerdict is TensionVerdict


async def test_updater_creates_verdict_page_linked_to_both_claims(
    tmp_db, explore_infra, tension_triple, mocker
):
    """End-to-end updater: scouts fire, synthesizer produces a verdict, a
    JUDGEMENT page is persisted with RELATED links to both tension claims
    and tagged with the tension_pair extra so dedup works next iteration."""
    q, claim_a, claim_b = tension_triple
    scout_pages: dict = {}
    _install_scout_stubs(mocker, scout_pages)
    verdict = _canned_tension_verdict("a_survives")
    captured = _install_synth_stub(mocker, verdict)

    updater = ExploreTensionUpdater()
    context = ContextResult(
        context_text="stub context",
        working_page_ids=[q.id, claim_a.id, claim_b.id],
    )

    result = await updater.update_workspace(explore_infra, context)

    assert len(result.created_page_ids) == 1
    verdict_page = await tmp_db.get_page(result.created_page_ids[0])
    assert verdict_page is not None
    assert verdict_page.page_type == PageType.JUDGEMENT
    assert verdict_page.credence == verdict.confidence
    assert verdict_page.provenance_call_id == explore_infra.call.id
    pair = verdict_page.extra["tension_pair"]
    assert pair["question_id"] == q.id
    assert pair["claim_a_id"] == claim_a.id
    assert pair["claim_b_id"] == claim_b.id

    outgoing = await tmp_db.get_links_from(verdict_page.id)
    related_targets = {l.to_page_id for l in outgoing if l.link_type == LinkType.RELATED}
    assert related_targets == {claim_a.id, claim_b.id}

    assert "how_true" in scout_pages and len(scout_pages["how_true"]) == 1
    assert "how_false" in scout_pages and len(scout_pages["how_false"]) == 1
    assert captured["response_model"] is TensionVerdict
    assert "How-True Scout on Claim A" in captured["user_message"]
    assert "How-False Scout on Claim B" in captured["user_message"]


async def test_updater_creates_refining_claim_when_resolution_refined(
    tmp_db, explore_infra, tension_triple, mocker
):
    """When the synthesizer picks both_survive_refined, a refining CLAIM
    is also created and linked to the parent question as a consideration."""
    q, claim_a, claim_b = tension_triple
    scout_pages: dict = {}
    _install_scout_stubs(mocker, scout_pages)
    verdict = _refining_verdict()
    _install_synth_stub(mocker, verdict)

    updater = ExploreTensionUpdater()
    context = ContextResult(
        context_text="stub",
        working_page_ids=[q.id, claim_a.id, claim_b.id],
    )
    result = await updater.update_workspace(explore_infra, context)

    assert len(result.created_page_ids) == 2
    verdict_page_id, refining_id = result.created_page_ids
    refining = await tmp_db.get_page(refining_id)
    assert refining is not None
    assert refining.page_type == PageType.CLAIM
    assert refining.headline == "Short-term vs long-term split resolves the tension"

    refining_links = await tmp_db.get_links_from(refining_id)
    consideration_to_q = [
        l for l in refining_links if l.link_type == LinkType.CONSIDERATION and l.to_page_id == q.id
    ]
    assert len(consideration_to_q) == 1
    assert refining.extra["refines_tension"]["verdict_page_id"] == verdict_page_id


async def test_updater_raises_when_synthesizer_returns_none(
    tmp_db, explore_infra, tension_triple, mocker
):
    q, claim_a, claim_b = tension_triple
    _install_scout_stubs(mocker, {})

    async def fake(*args, **kwargs):
        return StructuredCallResult(parsed=None, response_text="(invalid)")

    mocker.patch("rumil.calls.explore_tension.structured_call", side_effect=fake)
    updater = ExploreTensionUpdater()
    context = ContextResult(context_text="stub", working_page_ids=[q.id, claim_a.id, claim_b.id])
    with pytest.raises(ValueError, match="no parseable verdict"):
        await updater.update_workspace(explore_infra, context)


async def test_explore_tension_call_wires_stages(tmp_db, tension_triple, explore_tension_call):
    q, _, _ = tension_triple
    runner = ExploreTensionCall(q.id, explore_tension_call, tmp_db)
    assert runner.call_type == CallType.EXPLORE_TENSION
    assert runner.context_builder.__class__.__name__ == "ExploreTensionContext"
    assert runner.workspace_updater.__class__.__name__ == "ExploreTensionUpdater"
    assert runner.closing_reviewer.__class__.__name__ == "StandardClosingReview"
    desc = runner.task_description()
    assert q.id in desc


async def test_explore_tension_call_params_must_include_triple(tmp_db, tension_triple):
    """Without tension_question_id/claim_a_id/claim_b_id in call_params the
    call refuses to run — it has no way to know which tension to adjudicate."""
    q, _, _ = tension_triple
    bad_call = Call(
        call_type=CallType.EXPLORE_TENSION,
        workspace=Workspace.RESEARCH,
        scope_page_id=q.id,
        status=CallStatus.PENDING,
        call_params={},
    )
    await tmp_db.save_call(bad_call)
    runner = ExploreTensionCall(q.id, bad_call, tmp_db)
    with pytest.raises(ValueError, match="tension_question_id"):
        runner.task_description()


def _make_state(question_id: str = "q") -> QuestionState:
    return QuestionState(
        question_id=question_id,
        budget_remaining=10,
        iteration=0,
        consideration_count=2,
        child_question_count=0,
        source_count=0,
        view=None,
        missing_credence_page_ids=[],
        missing_importance_item_ids=[],
        unjudged_child_question_ids=[],
        recent_call_types=[],
    )


async def test_policy_returns_dispatch_when_unexplored_candidate_exists(mocker):
    """TensionExplorationPolicy emits a DispatchCall(EXPLORE_TENSION) when
    there's an unexplored candidate on the root question."""
    candidate = TensionCandidate.make(
        "q",
        "claim-x",
        "claim-y",
        kind="direction_conflict",
        reason="conflict",
        confidence=1.0,
    )
    mocker.patch(
        "rumil.orchestrators.policy_layer.unexplored_tension_candidates",
        new=AsyncMock(return_value=[candidate]),
    )

    policy = TensionExplorationPolicy(emit_suggestion=False)
    db = MagicMock()
    db.run_id = "run"
    db.project_id = "proj"
    db.staged = False
    policy.bind_db(db)

    intent = await policy.decide(_make_state("q"))

    assert isinstance(intent, DispatchCall)
    assert intent.call_type == CallType.EXPLORE_TENSION
    assert intent.kwargs["tension_question_id"] == "q"
    assert intent.kwargs["tension_claim_a_id"] == candidate.claim_a_id
    assert intent.kwargs["tension_claim_b_id"] == candidate.claim_b_id
    assert intent.kwargs["tension_kind"] == "direction_conflict"


async def test_policy_returns_none_when_no_candidates(mocker):
    mocker.patch(
        "rumil.orchestrators.policy_layer.unexplored_tension_candidates",
        new=AsyncMock(return_value=[]),
    )
    policy = TensionExplorationPolicy(emit_suggestion=False)
    db = MagicMock()
    db.run_id = "run"
    db.project_id = "proj"
    db.staged = False
    policy.bind_db(db)
    assert await policy.decide(_make_state()) is None


async def test_policy_returns_none_when_db_not_bound(mocker):
    """Defensive: if bind_db was never called (bespoke caller forgot),
    the policy declines to fire rather than raising."""
    spy = mocker.patch(
        "rumil.orchestrators.policy_layer.unexplored_tension_candidates",
        new=AsyncMock(return_value=[]),
    )
    policy = TensionExplorationPolicy(emit_suggestion=False)
    assert await policy.decide(_make_state()) is None
    spy.assert_not_called()


async def test_policy_emits_suggestion_when_enabled(mocker):
    """emit_suggestion=True writes a pending RESOLVE_TENSION suggestion with
    the tension pair in the payload."""
    candidate = TensionCandidate.make(
        "q",
        "claim-x",
        "claim-y",
        kind="direction_conflict",
        reason="conflict-reason",
        confidence=1.0,
    )
    mocker.patch(
        "rumil.orchestrators.policy_layer.unexplored_tension_candidates",
        new=AsyncMock(return_value=[candidate]),
    )

    policy = TensionExplorationPolicy(emit_suggestion=True)
    db = MagicMock()
    db.run_id = "run"
    db.project_id = "proj"
    db.staged = False
    db.save_suggestion = AsyncMock()
    policy.bind_db(db)

    intent = await policy.decide(_make_state("q"))

    assert isinstance(intent, DispatchCall)
    db.save_suggestion.assert_awaited_once()
    saved: Suggestion = db.save_suggestion.await_args.args[0]
    assert saved.suggestion_type == SuggestionType.RESOLVE_TENSION
    assert saved.payload["question_id"] == "q"
    assert saved.payload["claim_a_id"] == candidate.claim_a_id
    assert saved.payload["claim_b_id"] == candidate.claim_b_id
    assert saved.payload["other_node_id"] == candidate.claim_b_id


async def test_policy_picks_highest_confidence_candidate(mocker):
    """When multiple unexplored candidates exist, the policy picks the one
    with highest confidence."""
    lo = TensionCandidate.make(
        "q", "a1", "a2", kind="semantic_contradiction", reason="lo", confidence=0.4
    )
    hi = TensionCandidate.make(
        "q", "b1", "b2", kind="direction_conflict", reason="hi", confidence=1.0
    )
    mocker.patch(
        "rumil.orchestrators.policy_layer.unexplored_tension_candidates",
        new=AsyncMock(return_value=[lo, hi]),
    )
    policy = TensionExplorationPolicy(emit_suggestion=False)
    db = MagicMock()
    db.run_id = "run"
    db.project_id = "proj"
    db.staged = False
    policy.bind_db(db)
    intent = await policy.decide(_make_state("q"))
    assert isinstance(intent, intent.__class__)
    assert isinstance(intent, DispatchCall)
    assert intent.kwargs["tension_claim_a_id"] == hi.claim_a_id
    assert intent.kwargs["tension_claim_b_id"] == hi.claim_b_id


def test_credence_threshold_default_is_six():
    """Sanity: the threshold constant matches the 'credence >= 6' design."""
    assert TENSION_CREDENCE_THRESHOLD == 6
