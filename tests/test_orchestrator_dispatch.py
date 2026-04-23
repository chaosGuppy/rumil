"""Tests for the orchestrator's dispatch execution loop."""

import uuid

import pytest

from rumil.calls.scout_analogies import ScoutAnalogiesCall
from rumil.calls.scout_c_cruxes import ScoutCCruxesCall
from rumil.calls.scout_c_how_false import ScoutCHowFalseCall
from rumil.calls.scout_c_how_true import ScoutCHowTrueCall
from rumil.calls.scout_c_relevant_evidence import ScoutCRelevantEvidenceCall
from rumil.calls.scout_c_robustify import ScoutCRobustifyCall
from rumil.calls.scout_c_strengthen import ScoutCStrengthenCall
from rumil.calls.scout_c_stress_test_cases import ScoutCStressTestCasesCall
from rumil.calls.scout_deep_questions import ScoutDeepQuestionsCall
from rumil.calls.scout_estimates import ScoutEstimatesCall
from rumil.calls.scout_factchecks import ScoutFactchecksCall
from rumil.calls.scout_hypotheses import ScoutHypothesesCall
from rumil.calls.scout_paradigm_cases import ScoutParadigmCasesCall
from rumil.calls.scout_subquestions import ScoutSubquestionsCall
from rumil.calls.scout_web_questions import ScoutWebQuestionsCall
from rumil.database import DB
from rumil.models import (
    AssessDispatchPayload,
    BaseDispatchPayload,
    CallType,
    CreateViewDispatchPayload,
    Dispatch,
    ScoutAnalogiesDispatchPayload,
    ScoutCCruxesDispatchPayload,
    ScoutCHowFalseDispatchPayload,
    ScoutCHowTrueDispatchPayload,
    ScoutCRelevantEvidenceDispatchPayload,
    ScoutCRobustifyDispatchPayload,
    ScoutCStrengthenDispatchPayload,
    ScoutCStressTestCasesDispatchPayload,
    ScoutDeepQuestionsDispatchPayload,
    ScoutDispatchPayload,
    ScoutEstimatesDispatchPayload,
    ScoutFactchecksDispatchPayload,
    ScoutHypothesesDispatchPayload,
    ScoutParadigmCasesDispatchPayload,
    ScoutSubquestionsDispatchPayload,
    ScoutWebQuestionsDispatchPayload,
    WebResearchDispatchPayload,
)
from rumil.orchestrators import (
    BaseOrchestrator,
    PrioritizationResult,
    TwoPhaseOrchestrator,
)
from rumil.tracing.tracer import CallTrace, set_trace


class ScriptedOrchestrator(BaseOrchestrator):
    """Returns pre-scripted batches of dispatches, one per loop iteration."""

    def __init__(self, db, batches, call_id=None):
        super().__init__(db)
        self._batches = list(batches)
        self._index = 0
        self._call_id = call_id
        self.get_calls_count = 0

    async def run(self, root_question_id):
        await self._setup()
        try:
            for batch in self._batches:
                remaining = await self.db.budget_remaining()
                if remaining <= 0:
                    break
                self.get_calls_count += 1
                if not batch:
                    break
                await self._run_sequences(
                    [batch],
                    root_question_id,
                    self._call_id,
                )
        finally:
            await self._teardown()


def _scout_dispatch(question_id: str, **kwargs) -> Dispatch:
    return Dispatch(
        call_type=CallType.FIND_CONSIDERATIONS,
        payload=ScoutDispatchPayload(question_id=question_id, **kwargs),
    )


def _assess_dispatch(question_id: str, **kwargs) -> Dispatch:
    return Dispatch(
        call_type=CallType.ASSESS,
        payload=AssessDispatchPayload(question_id=question_id, **kwargs),
    )


@pytest.mark.integration
async def test_scout_dispatch_creates_scout_call(tmp_db, question_page):
    """A scout dispatch should produce a scout call in the DB."""
    orch = ScriptedOrchestrator(
        tmp_db,
        batches=[
            [_scout_dispatch(question_page.id, max_rounds=1)],
        ],
    )
    await orch.run(question_page.id)

    rows = (
        await tmp_db.client.table("calls")
        .select("call_type")
        .eq("run_id", tmp_db.run_id)
        .eq("call_type", "find_considerations")
        .execute()
    )
    assert len(rows.data) >= 1


@pytest.mark.integration
async def test_assess_dispatch_creates_assess_call(tmp_db, question_page):
    """An assess dispatch should produce an assess call in the DB."""
    orch = ScriptedOrchestrator(
        tmp_db,
        batches=[
            [_assess_dispatch(question_page.id)],
        ],
    )
    await orch.run(question_page.id)

    rows = (
        await tmp_db.client.table("calls")
        .select("call_type")
        .eq("run_id", tmp_db.run_id)
        .eq("call_type", "assess")
        .execute()
    )
    assert len(rows.data) >= 1


@pytest.mark.integration
async def test_budget_exhaustion_limits_dispatches(tmp_db, question_page):
    """Only dispatches that fit within the budget should execute."""
    await tmp_db.init_budget(1)
    orch = ScriptedOrchestrator(
        tmp_db,
        batches=[
            [_scout_dispatch(question_page.id, max_rounds=1)],
            [_scout_dispatch(question_page.id, max_rounds=1)],
            [_scout_dispatch(question_page.id, max_rounds=1)],
        ],
    )
    await orch.run(question_page.id)

    rows = (
        await tmp_db.client.table("calls")
        .select("call_type")
        .eq("run_id", tmp_db.run_id)
        .eq("call_type", "find_considerations")
        .execute()
    )
    assert len(rows.data) == 1


async def test_empty_dispatches_exits_loop(tmp_db, question_page):
    """When the orchestrator has no dispatches, the loop should exit."""
    orch = ScriptedOrchestrator(tmp_db, batches=[])
    await orch.run(question_page.id)

    rows = (
        await tmp_db.client.table("calls").select("call_type").eq("run_id", tmp_db.run_id).execute()
    )
    call_types = {r["call_type"] for r in rows.data}
    assert "find_considerations" not in call_types
    assert "assess" not in call_types


@pytest.mark.integration
async def test_reprioritization_on_leftover_budget(tmp_db, question_page):
    """Orchestrator should process multiple batches when budget remains."""
    await tmp_db.init_budget(5)
    orch = ScriptedOrchestrator(
        tmp_db,
        batches=[
            [_scout_dispatch(question_page.id, max_rounds=1)],
            [_assess_dispatch(question_page.id)],
        ],
    )
    await orch.run(question_page.id)

    assert orch.get_calls_count >= 2


async def test_no_infinite_loop_when_nothing_spent(tmp_db, question_page):
    """If budget is 0 the loop should exit immediately, not spin."""
    await tmp_db.init_budget(0)
    orch = ScriptedOrchestrator(
        tmp_db,
        batches=[
            [_scout_dispatch(question_page.id, max_rounds=1)],
        ],
    )
    await orch.run(question_page.id)

    assert orch.get_calls_count == 0


@pytest.mark.integration
async def test_unresolvable_question_id_falls_back_to_root(tmp_db, question_page):
    """When a dispatch references a non-existent page, the root question is used."""
    fake_id = str(uuid.uuid4())
    orch = ScriptedOrchestrator(
        tmp_db,
        batches=[
            [_assess_dispatch(fake_id)],
        ],
    )
    await orch.run(question_page.id)

    rows = (
        await tmp_db.client.table("calls")
        .select("scope_page_id")
        .eq("run_id", tmp_db.run_id)
        .eq("call_type", "assess")
        .execute()
    )
    assert len(rows.data) == 1
    assert rows.data[0]["scope_page_id"] == question_page.id


@pytest.mark.integration
async def test_dispatch_executed_events_recorded(tmp_db, question_page):
    """DispatchExecutedEvent should be persisted to trace_json."""
    p_call = await tmp_db.create_call(
        CallType.PRIORITIZATION,
        scope_page_id=question_page.id,
    )
    trace = CallTrace(p_call.id, tmp_db)
    set_trace(trace)

    orch = ScriptedOrchestrator(
        tmp_db,
        batches=[[_assess_dispatch(question_page.id)]],
        call_id=p_call.id,
    )
    await orch.run(question_page.id)

    rows = await tmp_db.client.table("calls").select("trace_json").eq("id", p_call.id).execute()
    trace_json = rows.data[0]["trace_json"]
    dispatch_events = [e for e in trace_json if e.get("event") == "dispatch_executed"]
    assert len(dispatch_events) >= 1
    evt = dispatch_events[0]
    assert evt["index"] == 0
    assert evt["child_call_type"] == "assess"
    assert evt["question_id"] == question_page.id
    assert evt["child_call_id"] is not None


async def test_concurrent_dispatch_failure_recorded_in_trace(
    tmp_db,
    question_page,
    mocker,
):
    """When a dispatch raises during the TwoPhaseOrchestrator's concurrent
    gather, an ErrorEvent should be recorded on the prioritization call's trace.
    """
    p_call = await tmp_db.create_call(
        CallType.PRIORITIZATION,
        scope_page_id=question_page.id,
    )

    orch = TwoPhaseOrchestrator(tmp_db)
    orch._call_id = p_call.id

    dispatches = [[_assess_dispatch(question_page.id)]]
    call_count = 0

    async def fake_get_next_batch(question_id, budget, parent_call_id=None, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return PrioritizationResult(
                dispatch_sequences=dispatches,
                call_id=p_call.id,
            )
        return PrioritizationResult(dispatch_sequences=[])

    mocker.patch.object(orch, "_get_next_batch", side_effect=fake_get_next_batch)
    mocker.patch.object(
        DB,
        "resolve_page_id",
        side_effect=ConnectionError("Simulated connection failure"),
    )

    await orch.run(question_page.id)

    rows = await tmp_db.client.table("calls").select("trace_json").eq("id", p_call.id).execute()
    trace_json = rows.data[0]["trace_json"]
    error_events = [e for e in trace_json if e.get("event") == "error"]
    assert len(error_events) >= 1
    assert "Simulated connection failure" in error_events[0]["message"]


# ---------------------------------------------------------------------------
# Behavioral unit tests for _execute_dispatch.
#
# These tests pin the current dispatch routing: for each payload type, which
# underlying helper/call class is invoked, and which payload fields are
# forwarded. They are written against the pre-refactor isinstance chain in
# orchestrators/base.py so the refactor to a registry can be proved
# behavior-preserving. Tests mock the call-side helpers; no real LLM work
# happens. A real DB is used so resolve_page_id and page_label do not need
# to be mocked.
# ---------------------------------------------------------------------------


SCOUT_FAMILY_CASES: list[tuple[type, CallType, type]] = [
    (
        ScoutSubquestionsDispatchPayload,
        CallType.SCOUT_SUBQUESTIONS,
        ScoutSubquestionsCall,
    ),
    (ScoutEstimatesDispatchPayload, CallType.SCOUT_ESTIMATES, ScoutEstimatesCall),
    (ScoutHypothesesDispatchPayload, CallType.SCOUT_HYPOTHESES, ScoutHypothesesCall),
    (ScoutAnalogiesDispatchPayload, CallType.SCOUT_ANALOGIES, ScoutAnalogiesCall),
    (
        ScoutParadigmCasesDispatchPayload,
        CallType.SCOUT_PARADIGM_CASES,
        ScoutParadigmCasesCall,
    ),
    (ScoutFactchecksDispatchPayload, CallType.SCOUT_FACTCHECKS, ScoutFactchecksCall),
    (
        ScoutWebQuestionsDispatchPayload,
        CallType.SCOUT_WEB_QUESTIONS,
        ScoutWebQuestionsCall,
    ),
    (
        ScoutDeepQuestionsDispatchPayload,
        CallType.SCOUT_DEEP_QUESTIONS,
        ScoutDeepQuestionsCall,
    ),
    (ScoutCHowTrueDispatchPayload, CallType.SCOUT_C_HOW_TRUE, ScoutCHowTrueCall),
    (ScoutCHowFalseDispatchPayload, CallType.SCOUT_C_HOW_FALSE, ScoutCHowFalseCall),
    (ScoutCCruxesDispatchPayload, CallType.SCOUT_C_CRUXES, ScoutCCruxesCall),
    (
        ScoutCRelevantEvidenceDispatchPayload,
        CallType.SCOUT_C_RELEVANT_EVIDENCE,
        ScoutCRelevantEvidenceCall,
    ),
    (
        ScoutCStressTestCasesDispatchPayload,
        CallType.SCOUT_C_STRESS_TEST_CASES,
        ScoutCStressTestCasesCall,
    ),
    (ScoutCRobustifyDispatchPayload, CallType.SCOUT_C_ROBUSTIFY, ScoutCRobustifyCall),
    (
        ScoutCStrengthenDispatchPayload,
        CallType.SCOUT_C_STRENGTHEN,
        ScoutCStrengthenCall,
    ),
]


@pytest.fixture
def mocked_helpers(mocker):
    """Patch every helper that _execute_dispatch may delegate to.

    Patches are applied at the handler namespace (dispatch_handlers) because
    that is where the handlers import these names from. Patching at the
    import site (not the definition site) avoids leaking mocks into unrelated
    callers of the same helpers.
    """
    return {
        "find_considerations": mocker.patch(
            "rumil.orchestrators.dispatch_handlers.find_considerations_until_done",
            return_value=(0, ["child-fc-id"]),
        ),
        "assess": mocker.patch(
            "rumil.orchestrators.dispatch_handlers.assess_question",
            return_value="child-assess-id",
        ),
        "web_research": mocker.patch(
            "rumil.orchestrators.dispatch_handlers.web_research_question",
            return_value="child-web-id",
        ),
        "create_view": mocker.patch(
            "rumil.views.sectioned.create_view_for_question",
            return_value="child-view-id",
        ),
        "update_view": mocker.patch(
            "rumil.views.sectioned.update_view_for_question",
            return_value="child-update-view-id",
        ),
        "simple": mocker.patch.object(
            BaseOrchestrator,
            "_run_simple_call_dispatch",
            return_value="child-simple-id",
        ),
    }


async def _make_orch(db) -> "ScriptedOrchestrator":
    orch = ScriptedOrchestrator(db, batches=[])
    await orch._setup()
    return orch


async def test_execute_dispatch_find_considerations_forwards_fields(
    tmp_db,
    question_page,
    mocked_helpers,
):
    orch = await _make_orch(tmp_db)
    dispatch = Dispatch(
        call_type=CallType.FIND_CONSIDERATIONS,
        payload=ScoutDispatchPayload(
            question_id=question_page.id,
            reason="test-reason",
            context_page_ids=["ctx-1"],
            max_rounds=3,
            fruit_threshold=2,
        ),
    )

    resolved, child_id = await orch._execute_dispatch(
        dispatch,
        question_page.id,
        parent_call_id="parent-1",
        force=True,
        call_id="pre-1",
        sequence_id="seq-1",
        sequence_position=4,
    )

    assert resolved == question_page.id
    assert child_id == "child-fc-id"
    mocked_helpers["find_considerations"].assert_called_once()
    kwargs = mocked_helpers["find_considerations"].call_args.kwargs
    assert mocked_helpers["find_considerations"].call_args.args[0] == question_page.id
    assert kwargs["max_rounds"] == 3
    assert kwargs["fruit_threshold"] == 2
    assert kwargs["parent_call_id"] == "parent-1"
    assert kwargs["context_page_ids"] == ["ctx-1"]
    assert kwargs["force"] is True
    assert kwargs["call_id"] == "pre-1"
    assert kwargs["sequence_id"] == "seq-1"
    assert kwargs["sequence_position"] == 4
    mocked_helpers["assess"].assert_not_called()
    mocked_helpers["create_view"].assert_not_called()
    mocked_helpers["simple"].assert_not_called()


async def test_execute_dispatch_find_considerations_returns_none_when_no_child_ids(
    tmp_db,
    question_page,
    mocker,
):
    """When find_considerations_until_done returns an empty child list,
    _execute_dispatch should return None as the child_call_id."""
    mocker.patch(
        "rumil.orchestrators.dispatch_handlers.find_considerations_until_done",
        return_value=(0, []),
    )
    mocker.patch(
        "rumil.orchestrators.dispatch_handlers.assess_question",
        return_value="not-this",
    )

    orch = await _make_orch(tmp_db)
    dispatch = Dispatch(
        call_type=CallType.FIND_CONSIDERATIONS,
        payload=ScoutDispatchPayload(
            question_id=question_page.id,
            reason="no-children",
        ),
    )

    resolved, child_id = await orch._execute_dispatch(
        dispatch,
        question_page.id,
        parent_call_id=None,
    )

    assert resolved == question_page.id
    assert child_id is None


async def test_execute_dispatch_assess_without_view_calls_assess_question(
    tmp_db,
    question_page,
    mocked_helpers,
    mocker,
):
    mocker.patch.object(DB, "get_view_for_question", return_value=None)

    orch = await _make_orch(tmp_db)
    dispatch = Dispatch(
        call_type=CallType.ASSESS,
        payload=AssessDispatchPayload(
            question_id=question_page.id,
            reason="assess-reason",
            context_page_ids=["cpi"],
        ),
    )

    resolved, child_id = await orch._execute_dispatch(
        dispatch,
        question_page.id,
        parent_call_id="parent-2",
        force=False,
        call_id="pre-2",
        sequence_id="seq-2",
        sequence_position=1,
    )

    assert resolved == question_page.id
    assert child_id == "child-assess-id"
    mocked_helpers["assess"].assert_called_once()
    kwargs = mocked_helpers["assess"].call_args.kwargs
    assert mocked_helpers["assess"].call_args.args[0] == question_page.id
    assert kwargs["parent_call_id"] == "parent-2"
    assert kwargs["context_page_ids"] == ["cpi"]
    assert kwargs["force"] is False
    assert kwargs["call_id"] == "pre-2"
    assert kwargs["sequence_id"] == "seq-2"
    assert kwargs["sequence_position"] == 1
    mocked_helpers["create_view"].assert_not_called()
    mocked_helpers["simple"].assert_not_called()


async def test_execute_dispatch_assess_with_existing_view_redirects_to_create_view(
    tmp_db,
    question_page,
    mocked_helpers,
    mocker,
):
    """When the target question already has a view, assess should redirect
    to update_view_for_question (iterative view update) instead of assess_question."""
    mocker.patch.object(
        DB,
        "get_view_for_question",
        return_value={"id": "some-view-id"},
    )

    orch = await _make_orch(tmp_db)
    dispatch = Dispatch(
        call_type=CallType.ASSESS,
        payload=AssessDispatchPayload(
            question_id=question_page.id,
            reason="redirect-reason",
            context_page_ids=["ctx-a", "ctx-b"],
        ),
    )

    resolved, child_id = await orch._execute_dispatch(
        dispatch,
        question_page.id,
        parent_call_id="parent-3",
        force=True,
        call_id="pre-3",
    )

    assert resolved == question_page.id
    assert child_id == "child-update-view-id"
    mocked_helpers["update_view"].assert_called_once()
    kwargs = mocked_helpers["update_view"].call_args.kwargs
    assert mocked_helpers["update_view"].call_args.args[0] == question_page.id
    assert kwargs["parent_call_id"] == "parent-3"
    assert kwargs["context_page_ids"] == ["ctx-a", "ctx-b"]
    assert kwargs["force"] is True
    assert kwargs["call_id"] == "pre-3"
    mocked_helpers["assess"].assert_not_called()
    mocked_helpers["simple"].assert_not_called()


async def test_execute_dispatch_create_view_calls_create_view(
    tmp_db,
    question_page,
    mocked_helpers,
    mocker,
):
    # Ensure existing-view check doesn't short-circuit the assess path.
    # (CreateViewDispatchPayload doesn't go through the assess branch, but
    # guard anyway so the test is order-independent.)
    mocker.patch.object(DB, "get_view_for_question", return_value=None)

    orch = await _make_orch(tmp_db)
    dispatch = Dispatch(
        call_type=CallType.CREATE_VIEW,
        payload=CreateViewDispatchPayload(
            question_id=question_page.id,
            reason="view-reason",
            context_page_ids=["cv-1"],
        ),
    )

    resolved, child_id = await orch._execute_dispatch(
        dispatch,
        question_page.id,
        parent_call_id="parent-4",
        force=False,
        call_id="pre-4",
        sequence_id="seq-4",
        sequence_position=0,
    )

    assert resolved == question_page.id
    assert child_id == "child-view-id"
    mocked_helpers["create_view"].assert_called_once()
    kwargs = mocked_helpers["create_view"].call_args.kwargs
    assert mocked_helpers["create_view"].call_args.args[0] == question_page.id
    assert kwargs["parent_call_id"] == "parent-4"
    assert kwargs["context_page_ids"] == ["cv-1"]
    assert kwargs["force"] is False
    assert kwargs["call_id"] == "pre-4"
    assert kwargs["sequence_id"] == "seq-4"
    assert kwargs["sequence_position"] == 0
    mocked_helpers["assess"].assert_not_called()
    mocked_helpers["simple"].assert_not_called()


async def test_execute_dispatch_web_research_calls_web_research(
    tmp_db,
    question_page,
    mocked_helpers,
):
    orch = await _make_orch(tmp_db)
    dispatch = Dispatch(
        call_type=CallType.WEB_RESEARCH,
        payload=WebResearchDispatchPayload(
            question_id=question_page.id,
            reason="web-reason",
        ),
    )

    resolved, child_id = await orch._execute_dispatch(
        dispatch,
        question_page.id,
        parent_call_id="parent-5",
        force=True,
        call_id="pre-5",
        sequence_id="seq-5",
        sequence_position=2,
    )

    assert resolved == question_page.id
    assert child_id == "child-web-id"
    mocked_helpers["web_research"].assert_called_once()
    kwargs = mocked_helpers["web_research"].call_args.kwargs
    assert mocked_helpers["web_research"].call_args.args[0] == question_page.id
    assert kwargs["parent_call_id"] == "parent-5"
    assert kwargs["force"] is True
    assert kwargs["call_id"] == "pre-5"
    assert kwargs["sequence_id"] == "seq-5"
    assert kwargs["sequence_position"] == 2
    mocked_helpers["simple"].assert_not_called()


@pytest.mark.parametrize(
    "payload_cls,expected_call_type,expected_call_cls",
    SCOUT_FAMILY_CASES,
    ids=[c[1].value for c in SCOUT_FAMILY_CASES],
)
async def test_execute_dispatch_scout_family(
    payload_cls,
    expected_call_type,
    expected_call_cls,
    tmp_db,
    question_page,
    mocked_helpers,
):
    """Every scope-only scout payload should route to _run_simple_call_dispatch
    with the correct CallType and CallRunner class, forwarding max_rounds
    and fruit_threshold from the payload."""
    orch = await _make_orch(tmp_db)
    payload = payload_cls(
        question_id=question_page.id,
        reason="scout-reason",
        max_rounds=4,
        fruit_threshold=3,
    )
    dispatch = Dispatch(call_type=expected_call_type, payload=payload)

    resolved, child_id = await orch._execute_dispatch(
        dispatch,
        question_page.id,
        parent_call_id="parent-s",
        force=True,
        call_id="pre-s",
        sequence_id="seq-s",
        sequence_position=5,
    )

    assert resolved == question_page.id
    assert child_id == "child-simple-id"

    mocked_helpers["simple"].assert_called_once()
    call = mocked_helpers["simple"].call_args
    # _run_simple_call_dispatch signature: (question_id, call_type, cls, parent_call_id, ...)
    assert call.args[0] == question_page.id
    assert call.args[1] == expected_call_type
    assert call.args[2] is expected_call_cls
    assert call.args[3] == "parent-s"
    assert call.kwargs["force"] is True
    assert call.kwargs["call_id"] == "pre-s"
    assert call.kwargs["sequence_id"] == "seq-s"
    assert call.kwargs["sequence_position"] == 5
    assert call.kwargs["max_rounds"] == 4
    assert call.kwargs["fruit_threshold"] == 3
    # Other helpers should not have been touched.
    mocked_helpers["find_considerations"].assert_not_called()
    mocked_helpers["assess"].assert_not_called()
    mocked_helpers["web_research"].assert_not_called()
    mocked_helpers["create_view"].assert_not_called()


async def test_execute_dispatch_unknown_payload_type_returns_none(
    tmp_db,
    question_page,
    mocked_helpers,
):
    """A payload type that _execute_dispatch doesn't handle should leave
    child_call_id as None (current behavior — the chain simply falls through).
    This test exists to lock in that silent-fall-through behavior so the
    refactor doesn't accidentally introduce a KeyError."""

    class UnknownPayload(BaseDispatchPayload):
        pass

    orch = await _make_orch(tmp_db)
    dispatch = Dispatch(
        call_type=CallType.ASSESS,  # call_type field isn't what the chain keys on
        payload=UnknownPayload(question_id=question_page.id),
    )

    resolved, child_id = await orch._execute_dispatch(
        dispatch,
        question_page.id,
        parent_call_id=None,
    )

    assert resolved == question_page.id
    assert child_id is None
    for mock in mocked_helpers.values():
        mock.assert_not_called()
