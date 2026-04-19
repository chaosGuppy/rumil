import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio

from rumil.database import DB
from rumil.models import (
    AssessDispatchPayload,
    CallType,
    Dispatch,
    NudgeAuthorKind,
    NudgeDurability,
    NudgeKind,
    NudgeScope,
    NudgeStatus,
    RunNudge,
    ScoutEstimatesDispatchPayload,
    ScoutSubquestionsDispatchPayload,
)
from rumil.nudges import (
    build_applied_event,
    consume_one_shot,
    filter_dispatch_sequences,
    render_steering_context,
)


@pytest_asyncio.fixture
async def project_id():
    db = await DB.create(run_id=str(uuid.uuid4()))
    project, _ = await db.get_or_create_project(f"test-nudge-consumer-{uuid.uuid4().hex[:8]}")
    yield project.id
    await db._execute(db.client.table("projects").delete().eq("id", project.id))


@pytest_asyncio.fixture
async def db_with_run(project_id):
    db = await DB.create(run_id=str(uuid.uuid4()), project_id=project_id)
    await db.create_run(name="consumer-test", question_id=None)
    yield db
    await db._execute(db.client.table("run_nudges").delete().eq("run_id", db.run_id))
    await db._execute(db.client.table("runs").delete().eq("id", db.run_id))


def _make_nudge(
    *,
    kind: NudgeKind = NudgeKind.CONSTRAIN_DISPATCH,
    hard: bool = True,
    durability: NudgeDurability = NudgeDurability.ONE_SHOT,
    scope: NudgeScope | None = None,
    soft_text: str | None = None,
    created_at: datetime | None = None,
) -> RunNudge:
    return RunNudge(
        id=str(uuid.uuid4()),
        run_id="test-run",
        author_kind=NudgeAuthorKind.HUMAN,
        kind=kind,
        durability=durability,
        scope=scope or NudgeScope(),
        hard=hard,
        soft_text=soft_text,
        created_at=created_at or datetime.now(UTC),
    )


def _dispatch(call_type: CallType, question_id: str = "q1") -> Dispatch:
    payload_cls: type
    if call_type == CallType.ASSESS:
        payload_cls = AssessDispatchPayload
    elif call_type == CallType.SCOUT_SUBQUESTIONS:
        payload_cls = ScoutSubquestionsDispatchPayload
    elif call_type == CallType.SCOUT_ESTIMATES:
        payload_cls = ScoutEstimatesDispatchPayload
    else:
        raise ValueError(f"Unsupported call_type for test: {call_type}")
    return Dispatch(call_type=call_type, payload=payload_cls(question_id=question_id))


def test_filter_passes_through_when_no_hard_nudges():
    sequences = [[_dispatch(CallType.ASSESS), _dispatch(CallType.SCOUT_SUBQUESTIONS)]]
    soft = _make_nudge(kind=NudgeKind.INJECT_NOTE, hard=False, soft_text="hint")
    filtered, fired, dropped = filter_dispatch_sequences(sequences, [soft])
    assert len(filtered) == 1
    assert len(filtered[0]) == 2
    assert fired == []
    assert dropped == 0


def test_filter_drops_banned_call_types():
    sequences = [
        [_dispatch(CallType.ASSESS), _dispatch(CallType.SCOUT_SUBQUESTIONS)],
        [_dispatch(CallType.SCOUT_ESTIMATES)],
    ]
    ban = _make_nudge(scope=NudgeScope(call_types=["assess", "scout_estimates"]))
    filtered, fired, dropped = filter_dispatch_sequences(sequences, [ban])
    call_types = [d.call_type for seq in filtered for d in seq]
    assert call_types == [CallType.SCOUT_SUBQUESTIONS]
    assert {n.id for n in fired} == {ban.id}
    assert dropped == 2


def test_filter_drops_empty_sequences():
    sequences = [[_dispatch(CallType.ASSESS)]]
    ban = _make_nudge(scope=NudgeScope(call_types=["assess"]))
    filtered, _, dropped = filter_dispatch_sequences(sequences, [ban])
    assert filtered == []
    assert dropped == 1


def test_filter_respects_question_ids_scope():
    sequences = [
        [
            _dispatch(CallType.ASSESS, question_id="q1"),
            _dispatch(CallType.ASSESS, question_id="q2"),
        ],
    ]
    ban = _make_nudge(scope=NudgeScope(call_types=["assess"], question_ids=["q1"]))
    filtered, fired, dropped = filter_dispatch_sequences(sequences, [ban])
    remaining = [d.payload.question_id for seq in filtered for d in seq]
    assert remaining == ["q2"]
    assert dropped == 1
    assert {n.id for n in fired} == {ban.id}


def test_filter_ignores_soft_constrain():
    sequences = [[_dispatch(CallType.ASSESS)]]
    soft_ban = _make_nudge(
        hard=False,
        scope=NudgeScope(call_types=["assess"]),
    )
    filtered, fired, dropped = filter_dispatch_sequences(sequences, [soft_ban])
    assert len(filtered[0]) == 1
    assert fired == []
    assert dropped == 0


def test_filter_reports_each_fired_nudge_once():
    sequences = [[_dispatch(CallType.ASSESS), _dispatch(CallType.ASSESS)]]
    ban = _make_nudge(scope=NudgeScope(call_types=["assess"]))
    _, fired, dropped = filter_dispatch_sequences(sequences, [ban])
    assert len(fired) == 1
    assert dropped == 2


def test_render_steering_context_newest_first():
    t0 = datetime(2026, 4, 19, 12, 0, tzinfo=UTC)
    old = _make_nudge(kind=NudgeKind.INJECT_NOTE, hard=False, soft_text="old note", created_at=t0)
    new = _make_nudge(
        kind=NudgeKind.INJECT_NOTE,
        hard=False,
        soft_text="new note",
        created_at=datetime(2026, 4, 19, 13, 0, tzinfo=UTC),
    )
    rendered = render_steering_context([old, new])
    assert "new note" in rendered
    assert "old note" in rendered
    assert rendered.index("new note") < rendered.index("old note")


def test_render_steering_context_empty_when_no_soft():
    hard = _make_nudge(scope=NudgeScope(call_types=["assess"]))
    assert render_steering_context([hard]) == ""


def test_render_steering_context_includes_scope_metadata():
    n = _make_nudge(
        kind=NudgeKind.INJECT_NOTE,
        hard=False,
        soft_text="hint",
        scope=NudgeScope(call_types=["web_research"], question_ids=["q1", "q2"]),
    )
    rendered = render_steering_context([n])
    assert "web_research" in rendered
    assert "q1" in rendered


def test_build_applied_event_shape():
    hard = _make_nudge(scope=NudgeScope(call_types=["assess"]))
    soft = _make_nudge(kind=NudgeKind.INJECT_NOTE, hard=False, soft_text="focus")
    event = build_applied_event(
        phase="orchestrator_dispatch",
        fired_hard=[hard],
        fired_soft=[soft],
        dropped_count=2,
    )
    assert event.phase == "orchestrator_dispatch"
    assert event.filtered_dispatch_count == 2
    assert len(event.applied) == 2
    assert event.applied[0].effect == "hard_filter"
    assert event.applied[1].effect == "context_injection"


async def test_consume_one_shot_flips_status(db_with_run):
    one_shot = await db_with_run.nudges.create_nudge(
        run_id=db_with_run.run_id,
        kind=NudgeKind.CONSTRAIN_DISPATCH,
        durability=NudgeDurability.ONE_SHOT,
        author_kind=NudgeAuthorKind.HUMAN,
        scope=NudgeScope(call_types=["assess"]),
        hard=True,
    )
    persistent = await db_with_run.nudges.create_nudge(
        run_id=db_with_run.run_id,
        kind=NudgeKind.CONSTRAIN_DISPATCH,
        durability=NudgeDurability.PERSISTENT,
        author_kind=NudgeAuthorKind.HUMAN,
        scope=NudgeScope(call_types=["ingest"]),
        hard=True,
    )

    await consume_one_shot(db_with_run, [one_shot, persistent])

    still_one_shot = await db_with_run.nudges.get_nudge(one_shot.id)
    still_persistent = await db_with_run.nudges.get_nudge(persistent.id)
    assert still_one_shot is not None and still_one_shot.status == NudgeStatus.CONSUMED
    assert still_persistent is not None and still_persistent.status == NudgeStatus.ACTIVE
