"""Tests for the multi-source reputation substrate.

The substrate records raw, per-source, per-dimension scores. It must never
collapse at write time — consumers aggregate at query time. See
marketplace-thread/07-feedback.md and
marketplace-thread/13-reputation-governance.md.
"""

import uuid

import pytest
import pytest_asyncio

from rumil.ab_eval.runner import preference_to_score
from rumil.database import DB
from rumil.models import (
    Call,
    CallStatus,
    CallType,
    MoveType,
    Page,
    PageLayer,
    PageType,
    Workspace,
)
from rumil.moves import MOVES
from rumil.moves.base import MoveState


async def _make_db(project_id: str, staged: bool = False) -> DB:
    from datetime import UTC, datetime

    db = await DB.create(run_id=str(uuid.uuid4()), staged=staged)
    db.project_id = project_id
    if staged:
        db.snapshot_ts = datetime.max.replace(tzinfo=UTC)
    return db


async def _register_run(db: DB, config: dict | None = None) -> None:
    await db.create_run(name="test", question_id=None, config=config)


@pytest_asyncio.fixture
async def project_id():
    db = await DB.create(run_id=str(uuid.uuid4()))
    project = await db.get_or_create_project(f"test-reputation-{uuid.uuid4().hex[:8]}")
    yield project.id
    await db._execute(db.client.table("projects").delete().eq("id", project.id))


@pytest_asyncio.fixture
async def run_db(project_id):
    db = await _make_db(project_id, staged=False)
    await _register_run(db, config={"orchestrator": "two_phase"})
    await db.init_budget(100)
    yield db
    await db.delete_run_data()


async def test_record_and_read_roundtrip(run_db):
    await run_db.record_reputation_event(
        source="eval_agent",
        dimension="consistency",
        score=2.5,
    )

    events = await run_db.get_reputation_events()
    assert len(events) == 1
    ev = events[0]
    assert ev.source == "eval_agent"
    assert ev.dimension == "consistency"
    assert ev.score == 2.5
    assert ev.run_id == run_db.run_id
    assert ev.project_id == run_db.project_id
    assert ev.staged is False


async def test_optional_fields_persist(run_db):
    await run_db.record_reputation_event(
        source="eval_agent",
        dimension="general_quality",
        score=-1.0,
        orchestrator="two_phase",
        task_shape={"kind": "factual_lookup", "depth": 3},
        source_call_id="call-abc-123",
        extra={"preference_label": "B slightly preferred"},
    )

    events = await run_db.get_reputation_events()
    assert len(events) == 1
    ev = events[0]
    assert ev.orchestrator == "two_phase"
    assert ev.task_shape == {"kind": "factual_lookup", "depth": 3}
    assert ev.source_call_id == "call-abc-123"
    assert ev.extra == {"preference_label": "B slightly preferred"}


async def test_filter_by_source_dimension_orchestrator(run_db):
    await run_db.record_reputation_event(
        source="eval_agent", dimension="consistency", score=1.0, orchestrator="two_phase"
    )
    await run_db.record_reputation_event(
        source="eval_agent", dimension="grounding", score=2.0, orchestrator="two_phase"
    )
    await run_db.record_reputation_event(
        source="human_feedback", dimension="issue_flag", score=1.0, orchestrator="distill_first"
    )

    by_source = await run_db.get_reputation_events(source="eval_agent")
    assert {e.dimension for e in by_source} == {"consistency", "grounding"}

    by_dim = await run_db.get_reputation_events(dimension="issue_flag")
    assert len(by_dim) == 1
    assert by_dim[0].source == "human_feedback"

    by_orch = await run_db.get_reputation_events(orchestrator="distill_first")
    assert len(by_orch) == 1
    assert by_orch[0].source == "human_feedback"


async def test_dimensions_are_not_collapsed(run_db):
    """Each (source, dimension) raw score is retained separately."""
    await run_db.record_reputation_event(source="eval_agent", dimension="consistency", score=3.0)
    await run_db.record_reputation_event(source="eval_agent", dimension="consistency", score=-1.0)
    await run_db.record_reputation_event(source="eval_agent", dimension="grounding", score=2.0)
    await run_db.record_reputation_event(
        source="human_feedback", dimension="consistency", score=1.0
    )

    all_events = await run_db.get_reputation_events()
    assert len(all_events) == 4

    eval_consistency = await run_db.get_reputation_events(
        source="eval_agent", dimension="consistency"
    )
    scores = sorted(e.score for e in eval_consistency)
    assert scores == [-1.0, 3.0]

    human_consistency = await run_db.get_reputation_events(
        source="human_feedback", dimension="consistency"
    )
    assert len(human_consistency) == 1
    assert human_consistency[0].score == 1.0


async def test_staged_isolation(project_id):
    """Staged runs' reputation events are invisible to non-staged readers."""
    baseline_db = await _make_db(project_id, staged=False)
    await _register_run(baseline_db)
    await baseline_db.init_budget(100)

    staged_db = await _make_db(project_id, staged=True)
    await _register_run(staged_db)
    await staged_db.init_budget(100)

    observer_db = await _make_db(project_id, staged=False)

    await baseline_db.record_reputation_event(
        source="eval_agent", dimension="consistency", score=1.0
    )
    await staged_db.record_reputation_event(source="eval_agent", dimension="consistency", score=5.0)

    staged_events = await staged_db.get_reputation_events()
    assert {e.score for e in staged_events} == {1.0, 5.0}

    observer_events = await observer_db.get_reputation_events()
    assert {e.score for e in observer_events} == {1.0}
    assert all(e.staged is False for e in observer_events)

    await baseline_db.delete_run_data()
    await staged_db.delete_run_data()
    await observer_db.delete_run_data()


async def test_two_staged_runs_cannot_see_each_other(project_id):
    """Two independent staged runs are isolated."""
    db_a = await _make_db(project_id, staged=True)
    await _register_run(db_a)
    await db_a.init_budget(100)

    db_b = await _make_db(project_id, staged=True)
    await _register_run(db_b)
    await db_b.init_budget(100)

    await db_a.record_reputation_event(source="eval_agent", dimension="consistency", score=1.0)
    await db_b.record_reputation_event(source="eval_agent", dimension="consistency", score=2.0)

    events_a = await db_a.get_reputation_events()
    events_b = await db_b.get_reputation_events()

    assert [e.score for e in events_a] == [1.0]
    assert [e.score for e in events_b] == [2.0]

    await db_a.delete_run_data()
    await db_b.delete_run_data()


async def test_stage_run_flips_reputation_events(project_id):
    """After stage_run(), the run's reputation events become invisible to baseline readers."""
    run_db = await _make_db(project_id, staged=False)
    await _register_run(run_db)
    await run_db.init_budget(100)

    observer = await _make_db(project_id, staged=False)

    await run_db.record_reputation_event(source="eval_agent", dimension="consistency", score=2.0)

    before = await observer.get_reputation_events()
    assert any(e.run_id == run_db.run_id for e in before)

    await observer.stage_run(run_db.run_id)

    after = await observer.get_reputation_events()
    assert all(e.run_id != run_db.run_id for e in after)

    staged_reader = await DB.create(run_id=run_db.run_id, staged=True)
    staged_reader.project_id = project_id
    staged_events = await staged_reader.get_reputation_events()
    assert any(e.run_id == run_db.run_id for e in staged_events)

    await run_db.delete_run_data()
    await observer.delete_run_data()


async def test_flag_funniness_records_human_feedback_reputation(run_db):
    """FLAG_FUNNINESS move fires the v1 human_feedback hook."""
    page = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        headline="A suspicious claim",
        content="Something seems off here.",
    )
    await run_db.save_page(page)

    call = Call(
        call_type=CallType.CLAUDE_CODE_DIRECT,
        workspace=Workspace.RESEARCH,
        status=CallStatus.RUNNING,
    )
    await run_db.save_call(call)

    state = MoveState(call, run_db)
    tool = MOVES[MoveType.FLAG_FUNNINESS].bind(state)
    await tool.fn({"page_id": page.id, "note": "This looks wrong"})

    events = await run_db.get_reputation_events(source="human_feedback")
    assert len(events) == 1
    ev = events[0]
    assert ev.dimension == "issue_flag"
    assert ev.score == 1.0
    assert ev.source_call_id == call.id
    assert ev.extra["flagged_page_id"] == page.id


async def test_flag_reputation_captures_subject_run_orchestrator(project_id):
    """The flag hook records the orchestrator of the run that created the flagged page."""
    authoring_db = await _make_db(project_id, staged=False)
    await _register_run(authoring_db, config={"orchestrator": "distill_first"})
    await authoring_db.init_budget(100)

    page = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        headline="authored claim",
        content="content",
    )
    await authoring_db.save_page(page)

    flagging_db = await _make_db(project_id, staged=False)
    await _register_run(flagging_db, config={"orchestrator": "two_phase"})
    await flagging_db.init_budget(100)

    call = Call(
        call_type=CallType.CLAUDE_CODE_DIRECT,
        workspace=Workspace.RESEARCH,
        status=CallStatus.RUNNING,
    )
    await flagging_db.save_call(call)

    state = MoveState(call, flagging_db)
    tool = MOVES[MoveType.FLAG_FUNNINESS].bind(state)
    await tool.fn({"page_id": page.id, "note": "hmm"})

    events = await flagging_db.get_reputation_events(source="human_feedback")
    assert len(events) == 1
    assert events[0].orchestrator == "distill_first"
    assert events[0].extra["subject_run_id"] == authoring_db.run_id

    await authoring_db.delete_run_data()
    await flagging_db.delete_run_data()


@pytest.mark.parametrize(
    ("preference", "expected_a", "expected_b"),
    [
        ("A strongly preferred", 3.0, -3.0),
        ("A somewhat preferred", 2.0, -2.0),
        ("A slightly preferred", 1.0, -1.0),
        ("Approximately indifferent between A and B", 0.0, 0.0),
        ("B slightly preferred", -1.0, 1.0),
        ("B somewhat preferred", -2.0, 2.0),
        ("B strongly preferred", -3.0, 3.0),
    ],
)
def test_preference_to_score_is_symmetric(preference, expected_a, expected_b):
    assert preference_to_score(preference, "A") == expected_a
    assert preference_to_score(preference, "B") == expected_b


def test_preference_to_score_handles_unknown():
    assert preference_to_score("Could not determine preference", "A") is None
    assert preference_to_score("Could not determine preference", "B") is None


async def test_eval_agent_hook_records_reputation_on_completion(run_db, mocker):
    """Completion of evaluate_run_with_agent fires the eval_agent reputation hook.

    Mocks run_sdk_agent to avoid calling the real LLM; the hook itself
    (_record_eval_reputation) and its integration with the runner are what's
    under test.
    """
    from dataclasses import dataclass

    from rumil.run_eval import runner as run_eval_runner
    from rumil.run_eval.agents import EvalAgentSpec

    question = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        headline="test question",
        content="body",
        extra={"task_shape": {"kind": "open_ended"}},
    )
    await run_db.save_page(question)

    spec = EvalAgentSpec(
        name="consistency",
        display_name="Consistency",
        prompt_file="run-eval-consistency.md",
    )

    @dataclass
    class _FakeResult:
        all_assistant_text: list[str]

    mocker.patch(
        "rumil.run_eval.runner.run_sdk_agent",
        return_value=_FakeResult(all_assistant_text=["report body"]),
    )
    mocker.patch(
        "rumil.run_eval.runner.explore_page_impl",
        return_value="graph context",
    )

    await run_eval_runner.evaluate_run_with_agent(
        spec,
        run_id=run_db.run_id,
        question_id=question.id,
        parent_db=run_db,
        broadcaster=None,
    )

    events = await run_db.get_reputation_events(source="eval_agent")
    assert len(events) == 1
    ev = events[0]
    assert ev.dimension == "consistency"
    assert ev.score == 1.0
    assert ev.task_shape == {"kind": "open_ended"}
    assert ev.orchestrator == "two_phase"
    assert ev.extra["subject_run_id"] == run_db.run_id
