import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from rumil.database import DB
from rumil.models import (
    NudgeAuthorKind,
    NudgeDurability,
    NudgeKind,
    NudgeScope,
    NudgeStatus,
)


@pytest_asyncio.fixture
async def project_id():
    db = await DB.create(run_id=str(uuid.uuid4()))
    project, _ = await db.get_or_create_project(f"test-nudges-{uuid.uuid4().hex[:8]}")
    yield project.id
    await db._execute(db.client.table("projects").delete().eq("id", project.id))


@pytest_asyncio.fixture
async def db_with_run(project_id):
    db = await DB.create(run_id=str(uuid.uuid4()), project_id=project_id)
    await db.create_run(name="test-run", question_id=None)
    yield db
    await db._execute(db.client.table("run_nudges").delete().eq("run_id", db.run_id))
    await db._execute(db.client.table("runs").delete().eq("id", db.run_id))


async def _create(
    db: DB,
    *,
    kind: NudgeKind = NudgeKind.INJECT_NOTE,
    durability: NudgeDurability = NudgeDurability.ONE_SHOT,
    scope: NudgeScope | None = None,
    soft_text: str | None = "hint",
    hard: bool = False,
):
    return await db.nudges.create_nudge(
        run_id=db.run_id,
        kind=kind,
        durability=durability,
        author_kind=NudgeAuthorKind.HUMAN,
        scope=scope or NudgeScope(),
        soft_text=soft_text,
        hard=hard,
    )


async def test_create_nudge_persists_fields(db_with_run):
    nudge = await _create(
        db_with_run,
        kind=NudgeKind.CONSTRAIN_DISPATCH,
        durability=NudgeDurability.PERSISTENT,
        scope=NudgeScope(call_types=["web_research"]),
        soft_text="avoid web",
        hard=True,
    )
    assert nudge.kind == NudgeKind.CONSTRAIN_DISPATCH
    assert nudge.durability == NudgeDurability.PERSISTENT
    assert nudge.scope.call_types == ["web_research"]
    assert nudge.hard is True
    assert nudge.status == NudgeStatus.ACTIVE

    refetched = await db_with_run.nudges.get_nudge(nudge.id)
    assert refetched is not None
    assert refetched.soft_text == "avoid web"
    assert refetched.author_kind == NudgeAuthorKind.HUMAN


async def test_list_nudges_filters_by_status(db_with_run):
    a = await _create(db_with_run, soft_text="a")
    b = await _create(db_with_run, soft_text="b")
    await db_with_run.nudges.revoke_nudge(a.id)

    active = await db_with_run.nudges.list_nudges_for_run(
        db_with_run.run_id, status=NudgeStatus.ACTIVE
    )
    assert {n.id for n in active} == {b.id}

    revoked = await db_with_run.nudges.list_nudges_for_run(
        db_with_run.run_id, status=NudgeStatus.REVOKED
    )
    assert {n.id for n in revoked} == {a.id}


async def test_empty_scope_matches_any_context(db_with_run):
    n = await _create(db_with_run)
    matched = await db_with_run.nudges.get_active_for_run(
        db_with_run.run_id, call_type="assess", question_ids=["q1"], call_id="c1"
    )
    assert n.id in {m.id for m in matched}


async def test_scope_call_types_filters_by_call_type(db_with_run):
    match = await _create(db_with_run, scope=NudgeScope(call_types=["web_research", "assess"]))
    miss = await _create(db_with_run, scope=NudgeScope(call_types=["ingest"]))

    matched = await db_with_run.nudges.get_active_for_run(db_with_run.run_id, call_type="assess")
    ids = {m.id for m in matched}
    assert match.id in ids
    assert miss.id not in ids


async def test_scope_call_types_no_match_when_call_type_absent(db_with_run):
    scoped = await _create(db_with_run, scope=NudgeScope(call_types=["assess"]))
    matched = await db_with_run.nudges.get_active_for_run(db_with_run.run_id, call_type=None)
    assert scoped.id not in {m.id for m in matched}


async def test_scope_question_ids_intersection(db_with_run):
    match = await _create(db_with_run, scope=NudgeScope(question_ids=["q1", "q2"]))
    miss = await _create(db_with_run, scope=NudgeScope(question_ids=["q9"]))
    matched = await db_with_run.nudges.get_active_for_run(
        db_with_run.run_id, question_ids=["q2", "q7"]
    )
    ids = {m.id for m in matched}
    assert match.id in ids
    assert miss.id not in ids


async def test_scope_call_id_exact_match(db_with_run):
    n = await _create(db_with_run, scope=NudgeScope(call_id="call-abc"))
    hit = await db_with_run.nudges.get_active_for_run(db_with_run.run_id, call_id="call-abc")
    miss = await db_with_run.nudges.get_active_for_run(db_with_run.run_id, call_id="call-xyz")
    assert n.id in {m.id for m in hit}
    assert n.id not in {m.id for m in miss}


async def test_expires_at_past_excludes_nudge(db_with_run):
    past = datetime.now(UTC) - timedelta(minutes=1)
    n = await _create(db_with_run, scope=NudgeScope(expires_at=past))
    matched = await db_with_run.nudges.get_active_for_run(db_with_run.run_id)
    assert n.id not in {m.id for m in matched}


async def test_get_active_returns_newest_first(db_with_run):
    first = await _create(db_with_run, soft_text="first")
    second = await _create(db_with_run, soft_text="second")
    third = await _create(db_with_run, soft_text="third")

    matched = await db_with_run.nudges.get_active_for_run(db_with_run.run_id)
    order = [m.id for m in matched]
    assert order.index(third.id) < order.index(second.id) < order.index(first.id)


async def test_revoke_nudge_flips_status(db_with_run):
    n = await _create(db_with_run)
    revoked = await db_with_run.nudges.revoke_nudge(n.id)
    assert revoked is not None
    assert revoked.status == NudgeStatus.REVOKED
    assert revoked.revoked_at is not None


async def test_mark_consumed_flips_status(db_with_run):
    n = await _create(db_with_run)
    consumed = await db_with_run.nudges.mark_consumed(n.id)
    assert consumed is not None
    assert consumed.status == NudgeStatus.CONSUMED
    assert consumed.consumed_at is not None


async def test_stage_run_flips_nudge_staged_flag(project_id):
    baseline = await DB.create(run_id=str(uuid.uuid4()), project_id=project_id)
    await baseline.create_run(name="to-be-staged", question_id=None)
    nudge = await _create(baseline, soft_text="pre-stage note")

    await baseline.stage_run(baseline.run_id)

    observer = await DB.create(run_id=str(uuid.uuid4()), project_id=project_id)
    observer_list = await observer.nudges.list_nudges_for_run(baseline.run_id)
    assert nudge.id not in {n.id for n in observer_list}

    raw = await baseline._execute(
        baseline.client.table("run_nudges").select("staged").eq("id", nudge.id)
    )
    assert raw.data and raw.data[0]["staged"] is True

    await baseline._execute(baseline.client.table("run_nudges").delete().eq("id", nudge.id))
    await baseline._execute(baseline.client.table("runs").delete().eq("id", baseline.run_id))


async def test_staged_run_sees_own_and_baseline_nudges(project_id):
    baseline = await DB.create(run_id=str(uuid.uuid4()), project_id=project_id)
    await baseline.create_run(name="baseline-run", question_id=None)

    staged = await DB.create(run_id=str(uuid.uuid4()), project_id=project_id, staged=True)
    await staged.create_run(name="staged-run", question_id=None)

    baseline_nudge = await _create(baseline, soft_text="baseline")
    staged_nudge = await _create(staged, soft_text="staged")

    baseline_list = await baseline.nudges.list_nudges_for_run(baseline.run_id)
    assert {n.id for n in baseline_list} == {baseline_nudge.id}

    staged_list = await staged.nudges.list_nudges_for_run(staged.run_id)
    assert {n.id for n in staged_list} == {staged_nudge.id}

    observer = await DB.create(run_id=str(uuid.uuid4()), project_id=project_id)
    observer_list = await observer.nudges.list_nudges_for_run(staged.run_id)
    assert staged_nudge.id not in {n.id for n in observer_list}

    for db, run_id in [
        (baseline, baseline.run_id),
        (staged, staged.run_id),
    ]:
        await db._execute(db.client.table("run_nudges").delete().eq("run_id", run_id))
        await db._execute(db.client.table("runs").delete().eq("id", run_id))
