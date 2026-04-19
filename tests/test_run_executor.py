"""RunExecutor read + write helpers cover the status-transition surface
that dispatch paths opt into while the full executor.start() refactor
is pending.
"""

from __future__ import annotations

import pytest_asyncio

from rumil.run_executor import RunExecutor, RunStatus


@pytest_asyncio.fixture
async def run_db(tmp_db):
    await tmp_db.create_run(name="executor-transitions", question_id=None, config={})
    return tmp_db


async def test_status_returns_none_for_unknown_run(tmp_db):
    ex = RunExecutor(tmp_db)
    assert await ex.status("does-not-exist") is None


async def test_status_default_is_pending(run_db):
    ex = RunExecutor(run_db)
    view = await ex.status(run_db.run_id)
    assert view is not None
    assert view.status == RunStatus.PENDING
    assert view.started_at is None
    assert view.finished_at is None


async def test_mark_started_transitions_pending(run_db):
    ex = RunExecutor(run_db)
    await ex.mark_started(run_db.run_id)
    view = await ex.status(run_db.run_id)
    assert view is not None
    assert view.status == RunStatus.RUNNING
    assert view.started_at is not None


async def test_mark_complete_sets_finished_and_cost(run_db):
    ex = RunExecutor(run_db)
    await ex.mark_started(run_db.run_id)
    await ex.mark_complete(run_db.run_id, cost_usd_cents=1234)
    view = await ex.status(run_db.run_id)
    assert view is not None
    assert view.status == RunStatus.COMPLETE
    assert view.finished_at is not None
    assert float(view.cost_usd) == 12.34


async def test_mark_failed_records_reason(run_db):
    ex = RunExecutor(run_db)
    await ex.mark_failed(run_db.run_id, reason="exchange exploded")
    view = await ex.status(run_db.run_id)
    assert view is not None
    assert view.status == RunStatus.FAILED
    assert view.cancel_reason == "exchange exploded"


async def test_mark_cancelled_records_reason(run_db):
    ex = RunExecutor(run_db)
    await ex.mark_cancelled(run_db.run_id, reason="user pressed cancel")
    view = await ex.status(run_db.run_id)
    assert view is not None
    assert view.status == RunStatus.CANCELLED
    assert view.cancel_reason == "user pressed cancel"


async def test_mark_started_is_idempotent_only_on_pending(run_db):
    ex = RunExecutor(run_db)
    await ex.mark_started(run_db.run_id)
    first = await ex.status(run_db.run_id)
    assert first is not None and first.started_at is not None
    started_at = first.started_at

    await ex.mark_started(run_db.run_id)
    second = await ex.status(run_db.run_id)
    assert second is not None
    assert second.started_at == started_at
