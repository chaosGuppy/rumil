import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio

from rumil.alerts import (
    DEFAULT_RULES,
    evaluate_alerts,
    resolve_config_for_kind,
)
from rumil.database import DB
from rumil.models import AlertConfig, AlertKind


@pytest_asyncio.fixture
async def project_id():
    db = await DB.create(run_id=str(uuid.uuid4()))
    project, _ = await db.get_or_create_project(f"test-alerts-{uuid.uuid4().hex[:8]}")
    yield project.id
    await db._execute(db.client.table("projects").delete().eq("id", project.id))


@pytest_asyncio.fixture
async def db_with_run(project_id):
    db = await DB.create(run_id=str(uuid.uuid4()), project_id=project_id)
    await db.create_run(name="alert-test", question_id=None)
    yield db
    await db._execute(db.client.table("alert_configs").delete().eq("run_id", db.run_id))
    await db._execute(db.client.table("call_costs").delete().eq("run_id", db.run_id))
    await db._execute(db.client.table("reputation_events").delete().eq("run_id", db.run_id))
    await db._execute(db.client.table("runs").delete().eq("id", db.run_id))


def _config(kind: AlertKind, **fields) -> AlertConfig:
    return AlertConfig(
        id=str(uuid.uuid4()),
        kind=kind,
        **fields,
    )


def test_resolve_falls_back_to_defaults_when_no_config():
    params, source = resolve_config_for_kind(AlertKind.STALL_TIMEOUT, [], "run-a", "proj-a")
    assert params == DEFAULT_RULES[AlertKind.STALL_TIMEOUT]
    assert source is None


def test_resolve_prefers_run_scoped_config():
    run_conf = _config(AlertKind.STALL_TIMEOUT, run_id="run-a", params={"minutes": 5})
    project_conf = _config(AlertKind.STALL_TIMEOUT, project_id="proj-a", params={"minutes": 30})
    params, source = resolve_config_for_kind(
        AlertKind.STALL_TIMEOUT, [run_conf, project_conf], "run-a", "proj-a"
    )
    assert params["minutes"] == 5
    assert source == run_conf.id


def test_resolve_uses_project_when_no_run_scoped():
    project_conf = _config(AlertKind.STALL_TIMEOUT, project_id="proj-a", params={"minutes": 30})
    params, source = resolve_config_for_kind(
        AlertKind.STALL_TIMEOUT, [project_conf], "run-a", "proj-a"
    )
    assert params["minutes"] == 30
    assert source == project_conf.id


def test_resolve_disabled_run_config_mutes_kind():
    run_conf = _config(
        AlertKind.STALL_TIMEOUT, run_id="run-a", params={"minutes": 5}, enabled=False
    )
    params, source = resolve_config_for_kind(AlertKind.STALL_TIMEOUT, [run_conf], "run-a", "proj-a")
    assert params == {}
    assert source is None


def test_resolve_merges_partial_params_with_defaults():
    run_conf = _config(AlertKind.CONFUSION_SPIKE, run_id="run-a", params={"threshold": 3.5})
    params, _ = resolve_config_for_kind(AlertKind.CONFUSION_SPIKE, [run_conf], "run-a", None)
    assert params["threshold"] == 3.5
    assert params["window_min"] == DEFAULT_RULES[AlertKind.CONFUSION_SPIKE]["window_min"]


async def test_no_alerts_fired_for_fresh_run(db_with_run):
    fired = await evaluate_alerts(db_with_run, db_with_run.run_id)
    assert fired == []


async def test_cost_threshold_fires_at_absolute_usd(db_with_run):
    await db_with_run.alert_configs.create(
        kind=AlertKind.COST_THRESHOLD,
        params={"absolute_usd": 1.0},
        run_id=db_with_run.run_id,
    )
    await db_with_run._execute(
        db_with_run.client.table("call_costs").insert(
            {
                "run_id": db_with_run.run_id,
                "call_id": str(uuid.uuid4()),
                "call_type": "assess",
                "usd": 1.5,
            }
        )
    )
    fired = await evaluate_alerts(db_with_run, db_with_run.run_id)
    assert len(fired) == 1
    assert fired[0].kind == AlertKind.COST_THRESHOLD
    assert fired[0].context["usd"] == 1.5


async def test_confusion_spike_requires_min_count(db_with_run):
    await db_with_run.alert_configs.create(
        kind=AlertKind.CONFUSION_SPIKE,
        params={"threshold": 1.0, "min_count": 3, "window_min": 60},
        run_id=db_with_run.run_id,
    )
    for _ in range(2):
        await db_with_run._execute(
            db_with_run.client.table("reputation_events").insert(
                {
                    "id": str(uuid.uuid4()),
                    "run_id": db_with_run.run_id,
                    "project_id": db_with_run.project_id,
                    "source": "confusion_scan",
                    "dimension": "confusion",
                    "score": 3.0,
                }
            )
        )
    fired = await evaluate_alerts(db_with_run, db_with_run.run_id)
    assert all(f.kind != AlertKind.CONFUSION_SPIKE for f in fired)


async def test_confusion_spike_fires_when_avg_over_threshold(db_with_run):
    await db_with_run.alert_configs.create(
        kind=AlertKind.CONFUSION_SPIKE,
        params={"threshold": 1.5, "min_count": 2, "window_min": 60},
        run_id=db_with_run.run_id,
    )
    for score in (2.0, 3.0):
        await db_with_run._execute(
            db_with_run.client.table("reputation_events").insert(
                {
                    "id": str(uuid.uuid4()),
                    "run_id": db_with_run.run_id,
                    "project_id": db_with_run.project_id,
                    "source": "confusion_scan",
                    "dimension": "confusion",
                    "score": score,
                }
            )
        )
    fired = await evaluate_alerts(db_with_run, db_with_run.run_id)
    confusion = [f for f in fired if f.kind == AlertKind.CONFUSION_SPIKE]
    assert len(confusion) == 1
    assert confusion[0].context["count"] == 2
