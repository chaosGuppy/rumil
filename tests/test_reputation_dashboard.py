"""Tests for the reputation dashboard API.

Covers:
- Grouping by (source, dimension, orchestrator)
- Filter params (orchestrator, source, dimension)
- Recent events endpoint (last-N, newest first)
- Sources stay separate (eval_agent vs human_feedback never collapsed)

Uses the real local Supabase via tmp_db, following the pattern in
tests/test_api.py and tests/test_reputation_events.py.
"""

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from rumil.api.app import app
from rumil.database import DB


@pytest.fixture
def api_client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest_asyncio.fixture
async def reputation_db(tmp_db):
    """tmp_db plus a registered run so reputation events can reference it."""
    await tmp_db.create_run(name="test-reputation", question_id=None, config={})
    return tmp_db


async def _seed_events(db: DB) -> None:
    """Seed a spread of events across sources/dimensions/orchestrators."""
    await db.record_reputation_event(
        source="eval_agent",
        dimension="consistency",
        score=2.0,
        orchestrator="two_phase",
    )
    await db.record_reputation_event(
        source="eval_agent",
        dimension="consistency",
        score=-1.0,
        orchestrator="two_phase",
    )
    await db.record_reputation_event(
        source="eval_agent",
        dimension="grounding",
        score=3.0,
        orchestrator="two_phase",
    )
    await db.record_reputation_event(
        source="eval_agent",
        dimension="consistency",
        score=1.0,
        orchestrator="distill_first",
    )
    await db.record_reputation_event(
        source="human_feedback",
        dimension="issue_flag",
        score=1.0,
        orchestrator="two_phase",
    )
    await db.record_reputation_event(
        source="human_feedback",
        dimension="issue_flag",
        score=1.0,
        orchestrator="two_phase",
    )


async def test_summary_groups_by_source_dimension_orchestrator(api_client, reputation_db):
    await _seed_events(reputation_db)

    resp = await api_client.get(f"/api/projects/{reputation_db.project_id}/reputation")
    assert resp.status_code == 200
    body = resp.json()

    assert body["project_id"] == reputation_db.project_id
    assert body["total_events"] == 6

    by_key = {(b["source"], b["dimension"], b["orchestrator"]): b for b in body["buckets"]}
    assert set(by_key.keys()) == {
        ("eval_agent", "consistency", "two_phase"),
        ("eval_agent", "consistency", "distill_first"),
        ("eval_agent", "grounding", "two_phase"),
        ("human_feedback", "issue_flag", "two_phase"),
    }

    consistency_two_phase = by_key[("eval_agent", "consistency", "two_phase")]
    assert consistency_two_phase["n_events"] == 2
    assert consistency_two_phase["mean_score"] == 0.5
    assert consistency_two_phase["min_score"] == -1.0
    assert consistency_two_phase["max_score"] == 2.0

    human = by_key[("human_feedback", "issue_flag", "two_phase")]
    assert human["n_events"] == 2
    assert human["mean_score"] == 1.0


async def test_summary_filters_by_orchestrator(api_client, reputation_db):
    await _seed_events(reputation_db)

    resp = await api_client.get(
        f"/api/projects/{reputation_db.project_id}/reputation",
        params={"orchestrator": "distill_first"},
    )
    assert resp.status_code == 200
    body = resp.json()

    assert body["total_events"] == 1
    assert len(body["buckets"]) == 1
    bucket = body["buckets"][0]
    assert bucket["source"] == "eval_agent"
    assert bucket["dimension"] == "consistency"
    assert bucket["orchestrator"] == "distill_first"


async def test_summary_filters_by_source(api_client, reputation_db):
    await _seed_events(reputation_db)

    resp = await api_client.get(
        f"/api/projects/{reputation_db.project_id}/reputation",
        params={"source": "human_feedback"},
    )
    assert resp.status_code == 200
    body = resp.json()

    assert body["total_events"] == 2
    assert [b["source"] for b in body["buckets"]] == ["human_feedback"]


async def test_summary_filters_by_dimension(api_client, reputation_db):
    await _seed_events(reputation_db)

    resp = await api_client.get(
        f"/api/projects/{reputation_db.project_id}/reputation",
        params={"dimension": "grounding"},
    )
    assert resp.status_code == 200
    body = resp.json()

    assert body["total_events"] == 1
    assert len(body["buckets"]) == 1
    assert body["buckets"][0]["dimension"] == "grounding"


async def test_summary_never_collapses_sources(api_client, reputation_db):
    """Even when sources share a dimension, they stay in separate buckets."""
    await reputation_db.record_reputation_event(
        source="eval_agent", dimension="consistency", score=3.0, orchestrator="two_phase"
    )
    await reputation_db.record_reputation_event(
        source="human_feedback",
        dimension="consistency",
        score=1.0,
        orchestrator="two_phase",
    )

    resp = await api_client.get(f"/api/projects/{reputation_db.project_id}/reputation")
    assert resp.status_code == 200
    buckets = resp.json()["buckets"]

    sources = {b["source"] for b in buckets}
    assert sources == {"eval_agent", "human_feedback"}
    assert len(buckets) == 2
    for b in buckets:
        assert b["dimension"] == "consistency"


async def test_events_endpoint_returns_last_n(api_client, reputation_db):
    for i in range(5):
        await reputation_db.record_reputation_event(
            source="eval_agent",
            dimension="consistency",
            score=float(i),
            orchestrator="two_phase",
        )

    resp = await api_client.get(
        f"/api/projects/{reputation_db.project_id}/reputation/events",
        params={"limit": 3},
    )
    assert resp.status_code == 200
    events = resp.json()

    assert len(events) == 3
    timestamps = [e["created_at"] for e in events]
    assert timestamps == sorted(timestamps, reverse=True)


async def test_events_endpoint_filters_pass_through(api_client, reputation_db):
    await _seed_events(reputation_db)

    resp = await api_client.get(
        f"/api/projects/{reputation_db.project_id}/reputation/events",
        params={"source": "human_feedback"},
    )
    assert resp.status_code == 200
    events = resp.json()

    assert len(events) == 2
    assert {e["source"] for e in events} == {"human_feedback"}


async def test_empty_project_returns_empty_summary(api_client, reputation_db):
    resp = await api_client.get(f"/api/projects/{reputation_db.project_id}/reputation")
    assert resp.status_code == 200
    body = resp.json()

    assert body["total_events"] == 0
    assert body["buckets"] == []


async def test_other_projects_events_not_included(api_client, reputation_db):
    """Events in a different project must not leak into the summary."""
    await _seed_events(reputation_db)

    other_db = await DB.create(run_id=str(uuid.uuid4()))
    try:
        other_project, _ = await other_db.get_or_create_project(
            f"test-rep-other-{uuid.uuid4().hex[:8]}"
        )
        other_db.project_id = other_project.id
        await other_db.create_run(name="other", question_id=None, config={})
        await other_db.record_reputation_event(
            source="eval_agent",
            dimension="consistency",
            score=99.0,
            orchestrator="two_phase",
        )

        resp = await api_client.get(f"/api/projects/{reputation_db.project_id}/reputation")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_events"] == 6
        all_scores = [b["max_score"] for b in body["buckets"]]
        assert 99.0 not in all_scores
    finally:
        await other_db.delete_run_data(delete_project=True)
        await other_db.close()
