"""Tests for the worldview + distill_first policy compositions.

These cover the migrations of the former WorldviewOrchestrator and
DistillFirstOrchestrator classes into compositions of Policy objects
driven by PolicyOrchestrator. DB and LLM are mocked entirely — the
tests exercise composition priority, state capture, and intent routing
without touching the database or hitting the network.
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from rumil.models import (
    CallType,
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
from rumil.orchestrators.policies import (
    EvaluateModePolicy,
    ExploreModePolicy,
    NoMoreCascadesPolicy,
    SeedViewPolicy,
    UpdateViewPolicy,
    cascade_policies,
    distill_first_policies,
    worldview_policies,
)
from rumil.orchestrators.policy_layer import (
    DispatchCall,
    PolicyOrchestrator,
    QuestionState,
    RunHelper,
    Terminate,
)


def _question(headline: str = "root?") -> Page:
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
        content="some claim",
        headline="Some claim",
        credence=credence,
    )


def _view(question_id: str) -> Page:
    return Page(
        page_type=PageType.VIEW,
        layer=PageLayer.WIKI,
        workspace=Workspace.RESEARCH,
        content="view content",
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


def _cascade_suggestion(target_page_id: str, source_page_id: str | None = None) -> Suggestion:
    return Suggestion(
        project_id=str(uuid.uuid4()),
        suggestion_type=SuggestionType.CASCADE_REVIEW,
        target_page_id=target_page_id,
        source_page_id=source_page_id,
        status=SuggestionStatus.PENDING,
        payload={"reasoning": "upstream updated"},
        created_at=datetime.now(UTC),
    )


def _make_db(
    *,
    budget: int = 10,
    considerations: list[tuple[Page, PageLink]] | None = None,
    children: list[Page] | None = None,
    view: Page | None = None,
    view_items: list[tuple[Page, PageLink]] | None = None,
    judgements_by_q: dict[str, list[Page]] | None = None,
    pending_suggestions: list[Suggestion] | None = None,
) -> MagicMock:
    db = MagicMock()
    db.run_id = str(uuid.uuid4())
    db.project_id = str(uuid.uuid4())
    db.staged = False
    db.get_considerations_for_question = AsyncMock(return_value=considerations or [])
    db.get_child_questions = AsyncMock(return_value=children or [])
    db.get_view_for_question = AsyncMock(return_value=view)
    db.get_view_items = AsyncMock(return_value=view_items or [])
    db.get_judgements_for_questions = AsyncMock(return_value=judgements_by_q or {})
    db.get_pending_suggestions = AsyncMock(return_value=pending_suggestions or [])
    db.get_recent_calls_for_question = AsyncMock(return_value=[])

    state = {"budget": budget}

    async def _remaining() -> int:
        return state["budget"]

    async def _get_budget() -> tuple[int, int]:
        return 100, 100 - state["budget"]

    db.budget_remaining = AsyncMock(side_effect=_remaining)
    db.get_budget = AsyncMock(side_effect=_get_budget)
    db._budget_state = state
    return db


@pytest.fixture
def patched_helpers(mocker):
    """Patch the common helpers the PolicyOrchestrator routes through."""
    mocker.patch(
        "rumil.orchestrators.policy_layer.check_triage_before_run",
        new_callable=AsyncMock,
        return_value=True,
    )
    find = mocker.patch(
        "rumil.orchestrators.policy_layer.find_considerations_until_done",
        new_callable=AsyncMock,
        return_value=(1, ["find-call-id"]),
    )
    assess = mocker.patch(
        "rumil.orchestrators.policy_layer.assess_question",
        new_callable=AsyncMock,
        return_value="assess-call-id",
    )
    create_view = mocker.patch(
        "rumil.orchestrators.policy_layer.create_view_for_question",
        new_callable=AsyncMock,
        return_value="create-view-call-id",
    )
    update_view = mocker.patch(
        "rumil.orchestrators.policy_layer.update_view_for_question",
        new_callable=AsyncMock,
        return_value="update-view-call-id",
    )
    web = mocker.patch(
        "rumil.orchestrators.policy_layer.web_research_question",
        new_callable=AsyncMock,
        return_value="web-call-id",
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


async def test_worldview_sparse_question_emits_explore_intent(patched_helpers):
    qid = "q-sparse"
    db = _make_db(budget=5)

    orch = PolicyOrchestrator(db, worldview_policies(db), max_iterations=1)
    await orch.run(qid)

    assert patched_helpers["find"].call_count == 1
    assert patched_helpers["find"].call_args.kwargs["question_id"] == qid
    assert patched_helpers["assess"].call_count == 0


async def test_worldview_pending_cascade_emits_evaluate_intent(patched_helpers):
    qid = "q-cascade"
    c1, l1 = _consideration_pair(qid, credence=5)
    c2, l2 = _consideration_pair(qid, credence=6)
    c3, l3 = _consideration_pair(qid, credence=7)

    suggestion = _cascade_suggestion(target_page_id=c1.id, source_page_id=c2.id)

    db = _make_db(
        budget=5,
        considerations=[(c1, l1), (c2, l2), (c3, l3)],
        pending_suggestions=[suggestion],
    )

    orch = PolicyOrchestrator(db, worldview_policies(db), max_iterations=1)
    await orch.run(qid)

    assert patched_helpers["assess"].call_count == 1
    assert patched_helpers["assess"].call_args.kwargs["question_id"] == c1.id
    assert patched_helpers["find"].call_count == 0


async def test_worldview_cascade_outside_scope_is_ignored(patched_helpers):
    qid = "q-ignore-cascade"
    c1, l1 = _consideration_pair(qid, credence=5)

    unrelated_suggestion = _cascade_suggestion(target_page_id="some-other-page")

    db = _make_db(
        budget=5,
        considerations=[(c1, l1)],
        pending_suggestions=[unrelated_suggestion],
    )

    orch = PolicyOrchestrator(db, worldview_policies(db), max_iterations=1)
    await orch.run(qid)

    assert patched_helpers["assess"].call_count == 0
    assert patched_helpers["find"].call_count == 1


async def test_worldview_view_health_beats_explore(patched_helpers):
    qid = "q-view-health"
    c_missing, l_missing = _consideration_pair(qid, credence=None)

    db = _make_db(
        budget=5,
        considerations=[(c_missing, l_missing)],
    )

    orch = PolicyOrchestrator(db, worldview_policies(db), max_iterations=1)
    await orch.run(qid)

    assert patched_helpers["assess"].call_count == 1
    assert patched_helpers["assess"].call_args.kwargs["question_id"] == c_missing.id
    assert patched_helpers["find"].call_count == 0


async def test_worldview_terminates_on_zero_budget(patched_helpers):
    db = _make_db(budget=0)
    orch = PolicyOrchestrator(db, worldview_policies(db))
    await orch.run("q")
    assert patched_helpers["find"].call_count == 0
    assert patched_helpers["assess"].call_count == 0


async def test_distill_first_no_view_emits_create_view(patched_helpers):
    qid = "q-no-view"
    db = _make_db(budget=5, view=None)

    orch = PolicyOrchestrator(db, distill_first_policies(), max_iterations=1)
    await orch.run(qid)

    assert patched_helpers["create_view"].call_count == 1
    assert patched_helpers["create_view"].call_args.kwargs["question_id"] == qid
    assert patched_helpers["assess"].call_count == 0
    assert patched_helpers["find"].call_count == 0


async def test_distill_first_post_create_view_fills_gaps(patched_helpers):
    qid = "q-has-view"
    view = _view(qid)
    c_missing, l_missing = _consideration_pair(qid, credence=None)
    c_ok, l_ok = _consideration_pair(qid, credence=5)

    db = _make_db(
        budget=5,
        view=view,
        considerations=[(c_missing, l_missing), (c_ok, l_ok)],
    )

    orch = PolicyOrchestrator(db, distill_first_policies(), max_iterations=1)
    await orch.run(qid)

    assert patched_helpers["create_view"].call_count == 0
    assert patched_helpers["assess"].call_count == 1
    assert patched_helpers["assess"].call_args.kwargs["question_id"] == c_missing.id


async def test_distill_first_terminates_on_zero_budget(patched_helpers):
    db = _make_db(budget=0, view=None)
    orch = PolicyOrchestrator(db, distill_first_policies())
    await orch.run("q")
    assert patched_helpers["create_view"].call_count == 0


async def test_seed_view_policy_returns_none_when_view_exists():
    qid = "q"
    view = _view(qid)
    state = QuestionState(
        question_id=qid,
        budget_remaining=5,
        iteration=0,
        consideration_count=0,
        child_question_count=0,
        source_count=0,
        view=view,
        missing_credence_page_ids=[],
        missing_importance_item_ids=[],
        unjudged_child_question_ids=[],
        recent_call_types=[],
    )
    assert await SeedViewPolicy().decide(state) is None


async def test_seed_view_policy_fires_when_no_view():
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
    intent = await SeedViewPolicy().decide(state)
    assert isinstance(intent, RunHelper)
    assert intent.name == "create_view_for_question"
    assert intent.kwargs["question_id"] == "q"


async def test_update_view_policy_fires_after_mutation():
    qid = "q"
    view = _view(qid)
    state = QuestionState(
        question_id=qid,
        budget_remaining=5,
        iteration=1,
        consideration_count=3,
        child_question_count=0,
        source_count=0,
        view=view,
        missing_credence_page_ids=[],
        missing_importance_item_ids=[],
        unjudged_child_question_ids=[],
        recent_call_types=[CallType.ASSESS],
    )
    intent = await UpdateViewPolicy().decide(state)
    assert isinstance(intent, RunHelper)
    assert intent.name == "update_view_for_question"


async def test_update_view_policy_skips_when_last_call_was_update_view():
    qid = "q"
    view = _view(qid)
    state = QuestionState(
        question_id=qid,
        budget_remaining=5,
        iteration=2,
        consideration_count=3,
        child_question_count=0,
        source_count=0,
        view=view,
        missing_credence_page_ids=[],
        missing_importance_item_ids=[],
        unjudged_child_question_ids=[],
        recent_call_types=[CallType.UPDATE_VIEW, CallType.ASSESS],
    )
    assert await UpdateViewPolicy().decide(state) is None


async def test_update_view_policy_skips_when_no_view():
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
        recent_call_types=[CallType.ASSESS],
    )
    assert await UpdateViewPolicy().decide(state) is None


async def test_evaluate_mode_policy_dedupes_processed_targets():
    qid = "q"
    c, link = _consideration_pair(qid, credence=5)
    suggestion = _cascade_suggestion(target_page_id=c.id)
    db = _make_db(
        considerations=[(c, link)],
        pending_suggestions=[suggestion],
    )
    state = QuestionState(
        question_id=qid,
        budget_remaining=5,
        iteration=0,
        consideration_count=1,
        child_question_count=0,
        source_count=0,
        view=None,
        missing_credence_page_ids=[],
        missing_importance_item_ids=[],
        unjudged_child_question_ids=[],
        recent_call_types=[],
        consideration_page_ids=(c.id,),
    )
    policy = EvaluateModePolicy(db)
    first = await policy.decide(state)
    assert isinstance(first, DispatchCall)
    assert first.kwargs["question_id"] == c.id

    second = await policy.decide(state)
    assert second is None


async def test_explore_mode_policy_yields_when_cascade_in_scope():
    qid = "q"
    c, link = _consideration_pair(qid, credence=5)
    suggestion = _cascade_suggestion(target_page_id=c.id)
    db = _make_db(
        considerations=[(c, link)],
        pending_suggestions=[suggestion],
    )
    state = QuestionState(
        question_id=qid,
        budget_remaining=5,
        iteration=0,
        consideration_count=1,
        child_question_count=0,
        source_count=0,
        view=None,
        missing_credence_page_ids=[],
        missing_importance_item_ids=[],
        unjudged_child_question_ids=[],
        recent_call_types=[],
        consideration_page_ids=(c.id,),
    )
    assert await ExploreModePolicy(db).decide(state) is None


async def test_explore_mode_policy_fires_when_sparse_and_no_cascade():
    qid = "q"
    db = _make_db()
    state = QuestionState(
        question_id=qid,
        budget_remaining=5,
        iteration=0,
        consideration_count=1,
        child_question_count=0,
        source_count=0,
        view=None,
        missing_credence_page_ids=[],
        missing_importance_item_ids=[],
        unjudged_child_question_ids=[],
        recent_call_types=[],
    )
    intent = await ExploreModePolicy(db).decide(state)
    assert isinstance(intent, RunHelper)
    assert intent.name == "find_considerations_until_done"


async def test_explore_mode_policy_yields_when_not_sparse():
    qid = "q"
    db = _make_db()
    state = QuestionState(
        question_id=qid,
        budget_remaining=5,
        iteration=0,
        consideration_count=5,
        child_question_count=2,
        source_count=0,
        view=None,
        missing_credence_page_ids=[],
        missing_importance_item_ids=[],
        unjudged_child_question_ids=[],
        recent_call_types=[],
    )
    assert await ExploreModePolicy(db).decide(state) is None


async def test_question_state_view_scope_includes_question_and_children():
    state = QuestionState(
        question_id="root",
        budget_remaining=5,
        iteration=0,
        consideration_count=2,
        child_question_count=1,
        source_count=0,
        view=None,
        missing_credence_page_ids=[],
        missing_importance_item_ids=[],
        unjudged_child_question_ids=[],
        recent_call_types=[],
        consideration_page_ids=("c1", "c2"),
        child_question_ids=("ch1",),
    )
    assert state.view_scope_page_ids == frozenset({"root", "c1", "c2", "ch1"})


async def test_distill_first_update_view_fires_after_assess_dispatch(patched_helpers):
    qid = "q-refresh"
    view = _view(qid)
    c_ok, l_ok = _consideration_pair(qid, credence=5)

    db = _make_db(
        budget=5,
        view=view,
        considerations=[(c_ok, l_ok)],
    )

    call_obj = MagicMock()
    call_obj.call_type = CallType.ASSESS.value
    db.get_recent_calls_for_question = AsyncMock(return_value=[call_obj])

    orch = PolicyOrchestrator(db, distill_first_policies(), max_iterations=1)
    await orch.run(qid)

    assert patched_helpers["update_view"].call_count == 1
    assert patched_helpers["update_view"].call_args.kwargs["question_id"] == qid


async def test_worldview_loop_handles_multi_iteration_with_terminate(patched_helpers):
    qid = "q-multi"
    c_missing, l_missing = _consideration_pair(qid, credence=None)
    c_ok, l_ok = _consideration_pair(qid, credence=5)
    c_ok2, l_ok2 = _consideration_pair(qid, credence=6)

    budget = {"v": 2}

    async def _remaining() -> int:
        return budget["v"]

    async def _get_budget() -> tuple[int, int]:
        return 100, 100 - budget["v"]

    db = _make_db(
        budget=2,
        considerations=[(c_missing, l_missing), (c_ok, l_ok), (c_ok2, l_ok2)],
    )
    db.budget_remaining = AsyncMock(side_effect=_remaining)
    db.get_budget = AsyncMock(side_effect=_get_budget)

    async def _drain_assess(**kwargs):
        budget["v"] = max(0, budget["v"] - 1)
        return "assess-id"

    patched_helpers["assess"].side_effect = _drain_assess

    orch = PolicyOrchestrator(db, worldview_policies(db), max_iterations=10)
    await orch.run(qid)

    assert patched_helpers["assess"].call_count >= 1


async def test_cascade_policies_single_suggestion_dispatches_assess(patched_helpers):
    qid = "q-cascade"
    target_id = "target-page"
    suggestion = _cascade_suggestion(target_page_id=target_id)

    db = _make_db(budget=5, pending_suggestions=[suggestion])

    orch = PolicyOrchestrator(db, cascade_policies(db), max_iterations=1)
    await orch.run(qid)

    assert patched_helpers["assess"].call_count == 1
    assert patched_helpers["assess"].call_args.kwargs["question_id"] == target_id


async def test_cascade_policies_empty_queue_terminates(patched_helpers):
    qid = "q-no-cascades"
    db = _make_db(budget=5, pending_suggestions=[])

    orch = PolicyOrchestrator(db, cascade_policies(db), max_iterations=10)
    await orch.run(qid)

    assert patched_helpers["assess"].call_count == 0


async def test_cascade_policies_skips_already_assessed_target(patched_helpers):
    qid = "q-dedupe"
    target_id = "target-page"
    suggestion = _cascade_suggestion(target_page_id=target_id)

    db = _make_db(budget=5, pending_suggestions=[suggestion])

    orch = PolicyOrchestrator(db, cascade_policies(db), max_iterations=5)
    await orch.run(qid)

    assert patched_helpers["assess"].call_count == 1
    assert patched_helpers["assess"].call_args.kwargs["question_id"] == target_id


async def test_no_more_cascades_policy_returns_none_when_pending_exists():
    suggestion = _cascade_suggestion(target_page_id="t")
    db = _make_db(pending_suggestions=[suggestion])
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
    assert await NoMoreCascadesPolicy(db).decide(state) is None


async def test_no_more_cascades_policy_terminates_when_empty():
    db = _make_db(pending_suggestions=[])
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
    intent = await NoMoreCascadesPolicy(db).decide(state)
    assert isinstance(intent, Terminate)
    assert "cascade_review" in intent.reason


async def test_no_more_cascades_policy_ignores_non_cascade_suggestions():
    non_cascade = Suggestion(
        suggestion_type=SuggestionType.RELEVEL,
        target_page_id="unrelated",
    )
    db = _make_db(pending_suggestions=[non_cascade])
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
    intent = await NoMoreCascadesPolicy(db).decide(state)
    assert isinstance(intent, Terminate)


async def test_cascade_variant_registered_in_factory(mocker):
    from rumil.orchestrators import Orchestrator
    from rumil.settings import override_settings

    fake_db = mocker.MagicMock()
    fake_db.get_budget = AsyncMock(return_value=(100, 0))
    fake_db.budget_remaining = AsyncMock(return_value=100)

    with override_settings(prioritizer_variant="cascade"):
        orch = Orchestrator(fake_db)

    assert isinstance(orch, PolicyOrchestrator)
