"""Tests for the dispatch-policy layer scaffolding.

LLM and DB are mocked entirely — these are pure unit tests over policy
composition, intent dispatch, and state capture. Helpers are patched at
the policy_layer module boundary so the tests exercise routing without
touching the database or hitting the network.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from rumil.models import (
    CallType,
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)
from rumil.orchestrators.policy_layer import (
    BudgetPolicy,
    DispatchCall,
    Intent,
    Policy,
    PolicyOrchestrator,
    QuestionState,
    RunHelper,
    SparseQuestionPolicy,
    Terminate,
    ViewHealthPolicy,
    two_phase_like_policies,
)


def _question(headline: str = "Q?") -> Page:
    return Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=headline,
        headline=headline,
    )


def _claim(credence: int | None = None) -> Page:
    return Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="claim",
        headline="claim",
        credence=credence,
    )


def _view(question_id: str) -> Page:
    return Page(
        page_type=PageType.VIEW,
        layer=PageLayer.WIKI,
        workspace=Workspace.RESEARCH,
        content="view",
        headline=f"View of {question_id[:8]}",
        sections=["key_findings"],
    )


def _consideration_pair(question_id: str, credence: int | None = None) -> tuple[Page, PageLink]:
    claim = _claim(credence=credence)
    link = PageLink(
        from_page_id=claim.id,
        to_page_id=question_id,
        link_type=LinkType.CONSIDERATION,
    )
    return claim, link


def _make_db(
    *,
    budget: int = 10,
    considerations: list[tuple[Page, PageLink]] | None = None,
    children: list[Page] | None = None,
    view: Page | None = None,
    view_items: list[tuple[Page, PageLink]] | None = None,
    judgements_by_q: dict[str, list[Page]] | None = None,
    source_count: int = 0,
) -> MagicMock:
    db = MagicMock()
    db.run_id = str(uuid.uuid4())
    db.project_id = str(uuid.uuid4())
    db.get_page = AsyncMock(return_value=None)
    db.get_considerations_for_question = AsyncMock(return_value=considerations or [])
    db.get_child_questions = AsyncMock(return_value=children or [])
    db.get_view_for_question = AsyncMock(return_value=view)
    db.get_view_items = AsyncMock(return_value=view_items or [])
    db.get_judgements_for_questions = AsyncMock(return_value=judgements_by_q or {})

    state = {"budget": budget}

    async def _remaining() -> int:
        return state["budget"]

    async def _get_budget() -> tuple[int, int]:
        return 100, 100 - state["budget"]

    db.budget_remaining = AsyncMock(side_effect=_remaining)
    db.get_budget = AsyncMock(side_effect=_get_budget)
    db._budget_state = state
    db.get_recent_calls_for_question = AsyncMock(return_value=[])

    async def _count_sources(db_arg, qid):
        return source_count

    db._source_count = source_count
    return db


@pytest.fixture
def patched_helpers(mocker):
    find = mocker.patch(
        "rumil.orchestrators.policy_layer.find_considerations_until_done",
        new_callable=AsyncMock,
        return_value=(1, ["call-id"]),
    )
    assess = mocker.patch(
        "rumil.orchestrators.policy_layer.assess_question",
        new_callable=AsyncMock,
        return_value="call-id",
    )
    create_view = mocker.patch(
        "rumil.orchestrators.policy_layer.create_view_for_question",
        new_callable=AsyncMock,
        return_value="call-id",
    )
    update_view = mocker.patch(
        "rumil.orchestrators.policy_layer.update_view_for_question",
        new_callable=AsyncMock,
        return_value="call-id",
    )
    web = mocker.patch(
        "rumil.orchestrators.policy_layer.web_research_question",
        new_callable=AsyncMock,
        return_value="call-id",
    )
    count_sources = mocker.patch(
        "rumil.orchestrators.common.count_sources_for_question",
        new_callable=AsyncMock,
        return_value=0,
    )
    return {
        "find": find,
        "assess": assess,
        "create_view": create_view,
        "update_view": update_view,
        "web": web,
        "count_sources": count_sources,
    }


async def test_budget_policy_terminates_when_budget_zero():
    state = QuestionState(
        question_id="q",
        budget_remaining=0,
        iteration=0,
        consideration_count=0,
        child_question_count=0,
        source_count=0,
        view=None,
        missing_credence_page_ids=[],
        missing_importance_item_ids=[],
        unjudged_child_question_ids=[],
        recent_call_types=[],
    )
    intent = await BudgetPolicy().decide(state)
    assert isinstance(intent, Terminate)
    assert intent.reason == "budget_exhausted"


async def test_budget_policy_returns_none_with_budget():
    state = QuestionState(
        question_id="q",
        budget_remaining=5,
        iteration=0,
        consideration_count=0,
        child_question_count=0,
        source_count=0,
        view=None,
        missing_credence_page_ids=[],
        missing_importance_item_ids=[],
        unjudged_child_question_ids=[],
        recent_call_types=[],
    )
    assert await BudgetPolicy().decide(state) is None


@pytest.mark.parametrize(
    ("page_count", "threshold", "expected_fires"),
    (
        (0, 3, True),
        (2, 3, True),
        (3, 3, False),
        (5, 3, False),
    ),
)
async def test_sparse_policy_fires_below_threshold(
    page_count: int, threshold: int, expected_fires: bool
):
    state = QuestionState(
        question_id="q",
        budget_remaining=10,
        iteration=0,
        consideration_count=page_count,
        child_question_count=0,
        source_count=0,
        view=None,
        missing_credence_page_ids=[],
        missing_importance_item_ids=[],
        unjudged_child_question_ids=[],
        recent_call_types=[],
    )
    intent = await SparseQuestionPolicy(threshold=threshold).decide(state)
    if expected_fires:
        assert isinstance(intent, RunHelper)
        assert intent.name == "find_considerations_until_done"
        assert intent.kwargs["question_id"] == "q"
    else:
        assert intent is None


async def test_view_health_policy_fires_on_missing_credence():
    state = QuestionState(
        question_id="q",
        budget_remaining=10,
        iteration=0,
        consideration_count=5,
        child_question_count=0,
        source_count=0,
        view=None,
        missing_credence_page_ids=["claim-missing"],
        missing_importance_item_ids=[],
        unjudged_child_question_ids=[],
        recent_call_types=[],
    )
    intent = await ViewHealthPolicy().decide(state)
    assert isinstance(intent, DispatchCall)
    assert intent.call_type == CallType.ASSESS
    assert intent.kwargs["question_id"] == "claim-missing"


async def test_view_health_policy_falls_back_to_unjudged_children():
    state = QuestionState(
        question_id="q",
        budget_remaining=10,
        iteration=0,
        consideration_count=5,
        child_question_count=2,
        source_count=0,
        view=None,
        missing_credence_page_ids=[],
        missing_importance_item_ids=[],
        unjudged_child_question_ids=["child-q"],
        recent_call_types=[],
    )
    intent = await ViewHealthPolicy().decide(state)
    assert isinstance(intent, DispatchCall)
    assert intent.call_type == CallType.ASSESS
    assert intent.kwargs["question_id"] == "child-q"


async def test_view_health_policy_returns_none_with_no_gaps():
    state = QuestionState(
        question_id="q",
        budget_remaining=10,
        iteration=0,
        consideration_count=5,
        child_question_count=0,
        source_count=0,
        view=None,
        missing_credence_page_ids=[],
        missing_importance_item_ids=[],
        unjudged_child_question_ids=[],
        recent_call_types=[],
    )
    assert await ViewHealthPolicy().decide(state) is None


class _RecordingPolicy(Policy):
    def __init__(self, name: str, intent: Intent | None):
        self.name = name
        self._intent = intent
        self.calls: list[QuestionState] = []

    async def decide(self, state: QuestionState) -> Intent | None:
        self.calls.append(state)
        return self._intent


async def test_priority_list_first_non_none_wins(patched_helpers):
    db = _make_db(budget=5)
    winner_intent = RunHelper(name="find_considerations_until_done", kwargs={"question_id": "q"})
    later_intent = DispatchCall(call_type=CallType.ASSESS, kwargs={"question_id": "q"})

    first = _RecordingPolicy("first", None)
    winner = _RecordingPolicy("winner", winner_intent)
    later = _RecordingPolicy("later", later_intent)
    budget = BudgetPolicy()

    orch = PolicyOrchestrator(db, [budget, first, winner, later], max_iterations=1)
    await orch.run("q")

    assert len(first.calls) == 1
    assert len(winner.calls) == 1
    assert len(later.calls) == 0
    assert patched_helpers["find"].call_count == 1
    assert patched_helpers["assess"].call_count == 0


async def test_terminate_intent_stops_loop(patched_helpers):
    db = _make_db(budget=100)
    stop = _RecordingPolicy("stop", Terminate(reason="manual"))
    follower = _RecordingPolicy("follower", None)
    orch = PolicyOrchestrator(db, [stop, follower])
    await orch.run("q")
    assert len(stop.calls) == 1
    assert patched_helpers["find"].call_count == 0
    assert patched_helpers["assess"].call_count == 0


async def test_budget_policy_first_terminates_immediately(patched_helpers):
    db = _make_db(budget=0)
    sparse = _RecordingPolicy("sparse", RunHelper(name="find_considerations_until_done"))
    orch = PolicyOrchestrator(db, [BudgetPolicy(), sparse])
    await orch.run("q")
    assert len(sparse.calls) == 0
    assert patched_helpers["find"].call_count == 0


async def test_none_from_all_policies_stops_loop(patched_helpers):
    db = _make_db(budget=5)
    p1 = _RecordingPolicy("p1", None)
    p2 = _RecordingPolicy("p2", None)
    orch = PolicyOrchestrator(db, [p1, p2])
    await orch.run("q")
    assert len(p1.calls) == 1
    assert len(p2.calls) == 1


async def test_dispatch_call_routes_to_assess(patched_helpers):
    db = _make_db(budget=5)
    intent = DispatchCall(call_type=CallType.ASSESS, kwargs={"question_id": "target"})
    one_shot = _RecordingPolicy("once", intent)
    orch = PolicyOrchestrator(db, [BudgetPolicy(), one_shot], max_iterations=1)
    await orch.run("q")
    assert patched_helpers["assess"].call_count == 1
    kwargs = patched_helpers["assess"].call_args.kwargs
    assert kwargs["question_id"] == "target"
    assert kwargs["db"] is db


async def test_dispatch_call_routes_to_web_research(patched_helpers):
    db = _make_db(budget=5)
    intent = DispatchCall(call_type=CallType.WEB_RESEARCH)
    once = _RecordingPolicy("once", intent)
    orch = PolicyOrchestrator(db, [BudgetPolicy(), once], max_iterations=1)
    await orch.run("q")
    assert patched_helpers["web"].call_count == 1


async def test_dispatch_call_raises_for_unwired_call_type(patched_helpers):
    db = _make_db(budget=5)
    intent = DispatchCall(call_type=CallType.INGEST)
    once = _RecordingPolicy("once", intent)
    orch = PolicyOrchestrator(db, [BudgetPolicy(), once], max_iterations=1)
    with pytest.raises(NotImplementedError):
        await orch.run("q")


async def test_run_helper_routes_to_named_helper(patched_helpers):
    db = _make_db(budget=5)
    intent = RunHelper(name="update_view_for_question", kwargs={"question_id": "q"})
    once = _RecordingPolicy("once", intent)
    orch = PolicyOrchestrator(db, [BudgetPolicy(), once], max_iterations=1)
    await orch.run("q")
    assert patched_helpers["update_view"].call_count == 1


async def test_run_helper_raises_for_unregistered_name(patched_helpers):
    db = _make_db(budget=5)
    intent = RunHelper(name="no_such_helper")
    once = _RecordingPolicy("once", intent)
    orch = PolicyOrchestrator(db, [BudgetPolicy(), once], max_iterations=1)
    with pytest.raises(NotImplementedError):
        await orch.run("q")


async def test_max_iterations_caps_loop(patched_helpers):
    db = _make_db(budget=100)
    always_fire = _RecordingPolicy(
        "forever",
        RunHelper(name="find_considerations_until_done", kwargs={"question_id": "q"}),
    )
    orch = PolicyOrchestrator(db, [always_fire], max_iterations=3)
    await orch.run("q")
    assert len(always_fire.calls) == 3
    assert patched_helpers["find"].call_count == 3


async def test_question_state_capture_populates_fields(patched_helpers):
    qid = "q1"
    c1, l1 = _consideration_pair(qid, credence=5)
    c2, l2 = _consideration_pair(qid, credence=None)
    child = _question("child?")
    view = _view(qid)
    db = _make_db(
        budget=7,
        considerations=[(c1, l1), (c2, l2)],
        children=[child],
        view=view,
        judgements_by_q={child.id: []},
    )
    state = await QuestionState.capture(db, qid, iteration=4)
    assert state.question_id == qid
    assert state.budget_remaining == 7
    assert state.iteration == 4
    assert state.consideration_count == 2
    assert state.child_question_count == 1
    assert state.page_count == 3
    assert state.view is view
    assert list(state.missing_credence_page_ids) == [c2.id]
    assert list(state.unjudged_child_question_ids) == [child.id]


async def test_question_state_capture_handles_no_view(patched_helpers):
    qid = "q1"
    db = _make_db(budget=5, view=None)
    state = await QuestionState.capture(db, qid, iteration=0)
    assert state.view is None
    assert list(state.missing_importance_item_ids) == []
    db.get_view_items.assert_not_called()


async def test_two_phase_like_composition_bootstraps_sparse_question(patched_helpers):
    db = _make_db(budget=3)
    orch = PolicyOrchestrator(db, two_phase_like_policies(), max_iterations=1)
    await orch.run("q")
    assert patched_helpers["find"].call_count == 1
    assert patched_helpers["assess"].call_count == 0


async def test_two_phase_like_composition_advances_to_view_health(patched_helpers):
    qid = "q1"
    c_missing, l_missing = _consideration_pair(qid, credence=None)
    c_ok, l_ok = _consideration_pair(qid, credence=7)
    c_ok2, l_ok2 = _consideration_pair(qid, credence=6)
    db = _make_db(
        budget=5,
        considerations=[(c_missing, l_missing), (c_ok, l_ok), (c_ok2, l_ok2)],
    )
    orch = PolicyOrchestrator(db, two_phase_like_policies(), max_iterations=1)
    await orch.run(qid)
    assert patched_helpers["find"].call_count == 0
    assert patched_helpers["assess"].call_count == 1
    assert patched_helpers["assess"].call_args.kwargs["question_id"] == c_missing.id


async def test_two_phase_like_composition_terminates_on_zero_budget(patched_helpers):
    db = _make_db(budget=0)
    orch = PolicyOrchestrator(db, two_phase_like_policies())
    await orch.run("q")
    assert patched_helpers["find"].call_count == 0
    assert patched_helpers["assess"].call_count == 0
