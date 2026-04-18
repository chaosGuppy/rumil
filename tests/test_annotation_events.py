"""Tests for the broad-surface annotation substrate.

The annotation_events table is append-only and never collapsed at write
time. Aggregation is query-time. Humans and models write to the same table;
the author_type column separates them. See
marketplace-thread/28-annotation-primitives.md.

Mirrors test_reputation_events.py style: uses the real local supabase DB,
opt-outs staged visibility with ``snapshot_ts=datetime.max`` so tests can
see their own baseline events without mutation-events plumbing.
"""

import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from rumil.api.app import app
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
from rumil.settings import override_settings


async def _make_db(project_id: str, staged: bool = False) -> DB:
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
    project = await db.get_or_create_project(f"test-annotations-{uuid.uuid4().hex[:8]}")
    yield project.id
    await db._execute(db.client.table("projects").delete().eq("id", project.id))


@pytest_asyncio.fixture
async def run_db(project_id):
    db = await _make_db(project_id, staged=False)
    await _register_run(db, config={"orchestrator": "two_phase"})
    await db.init_budget(100)
    yield db
    await db.delete_run_data()


@pytest.fixture
def api_client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def _make_page(db: DB) -> Page:
    page = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        headline="annotation target",
        content="A paragraph with some content that can be span-annotated.",
    )
    await db.save_page(page)
    return page


async def _make_call(db: DB) -> Call:
    call = Call(
        call_type=CallType.CLAUDE_CODE_DIRECT,
        workspace=Workspace.RESEARCH,
        status=CallStatus.RUNNING,
    )
    await db.save_call(call)
    return call


async def test_span_annotation_roundtrip(run_db):
    page = await _make_page(run_db)
    await run_db.record_annotation(
        annotation_type="span",
        author_type="human",
        author_id="alice",
        target_page_id=page.id,
        span_start=5,
        span_end=25,
        category="factual_error",
        note="This clause overreaches.",
    )
    anns = await run_db.get_annotations()
    assert len(anns) == 1
    ann = anns[0]
    assert ann.annotation_type == "span"
    assert ann.author_type == "human"
    assert ann.author_id == "alice"
    assert ann.target_page_id == page.id
    assert ann.span_start == 5
    assert ann.span_end == 25
    assert ann.category == "factual_error"
    assert ann.note == "This clause overreaches."
    assert ann.run_id == run_db.run_id
    assert ann.project_id == run_db.project_id


async def test_counterfactual_annotation_roundtrip(run_db):
    call = await _make_call(run_db)
    await run_db.record_annotation(
        annotation_type="counterfactual_tool_use",
        author_type="model",
        author_id=call.id,
        target_call_id=call.id,
        target_event_seq=7,
        note="should have fired web_research",
        payload={"alternative": "web_research on parent question"},
    )
    anns = await run_db.get_annotations()
    assert len(anns) == 1
    ann = anns[0]
    assert ann.annotation_type == "counterfactual_tool_use"
    assert ann.target_call_id == call.id
    assert ann.target_event_seq == 7
    assert ann.payload == {"alternative": "web_research on parent question"}


async def test_flag_annotation_roundtrip(run_db):
    page = await _make_page(run_db)
    await run_db.record_annotation(
        annotation_type="flag",
        author_type="human",
        author_id="bob",
        target_page_id=page.id,
        category="missing_consideration",
        note="whole-page feedback",
    )
    anns = await run_db.get_annotations()
    assert len(anns) == 1
    assert anns[0].annotation_type == "flag"
    assert anns[0].category == "missing_consideration"


async def test_endorsement_annotation_roundtrip(run_db):
    page = await _make_page(run_db)
    await run_db.record_annotation(
        annotation_type="endorsement",
        author_type="human",
        author_id="carol",
        target_page_id=page.id,
        note="This claim is well-supported.",
    )
    anns = await run_db.get_annotations()
    assert len(anns) == 1
    assert anns[0].annotation_type == "endorsement"
    assert anns[0].note == "This claim is well-supported."


async def test_filter_by_target_page_id(run_db):
    page_a = await _make_page(run_db)
    page_b = await _make_page(run_db)
    await run_db.record_annotation(
        annotation_type="span", author_type="human", author_id="u", target_page_id=page_a.id
    )
    await run_db.record_annotation(
        annotation_type="span", author_type="human", author_id="u", target_page_id=page_b.id
    )
    on_a = await run_db.get_annotations(target_page_id=page_a.id)
    on_b = await run_db.get_annotations(target_page_id=page_b.id)
    assert len(on_a) == 1
    assert on_a[0].target_page_id == page_a.id
    assert len(on_b) == 1
    assert on_b[0].target_page_id == page_b.id


async def test_filter_by_target_call_id(run_db):
    call_a = await _make_call(run_db)
    call_b = await _make_call(run_db)
    await run_db.record_annotation(
        annotation_type="counterfactual_tool_use",
        author_type="model",
        author_id=call_a.id,
        target_call_id=call_a.id,
        target_event_seq=0,
    )
    await run_db.record_annotation(
        annotation_type="counterfactual_tool_use",
        author_type="model",
        author_id=call_b.id,
        target_call_id=call_b.id,
        target_event_seq=0,
    )
    on_a = await run_db.get_annotations(target_call_id=call_a.id)
    assert len(on_a) == 1
    assert on_a[0].target_call_id == call_a.id


async def test_filter_by_author_type(run_db):
    page = await _make_page(run_db)
    call = await _make_call(run_db)
    await run_db.record_annotation(
        annotation_type="flag", author_type="human", author_id="u", target_page_id=page.id
    )
    await run_db.record_annotation(
        annotation_type="span", author_type="model", author_id=call.id, target_page_id=page.id
    )
    humans = await run_db.get_annotations(author_type="human")
    models = await run_db.get_annotations(author_type="model")
    assert {a.annotation_type for a in humans} == {"flag"}
    assert {a.annotation_type for a in models} == {"span"}


async def test_filter_by_annotation_type(run_db):
    page = await _make_page(run_db)
    await run_db.record_annotation(
        annotation_type="span", author_type="human", author_id="u", target_page_id=page.id
    )
    await run_db.record_annotation(
        annotation_type="endorsement", author_type="human", author_id="u", target_page_id=page.id
    )
    only_span = await run_db.get_annotations(annotation_type="span")
    assert len(only_span) == 1
    assert only_span[0].annotation_type == "span"


async def test_staged_isolation(project_id):
    """Staged runs' annotations are invisible to non-staged readers."""
    baseline_db = await _make_db(project_id, staged=False)
    await _register_run(baseline_db)
    await baseline_db.init_budget(100)

    staged_db = await _make_db(project_id, staged=True)
    await _register_run(staged_db)
    await staged_db.init_budget(100)

    observer_db = await _make_db(project_id, staged=False)

    baseline_page = await _make_page(baseline_db)
    staged_page = await _make_page(staged_db)

    await baseline_db.record_annotation(
        annotation_type="flag",
        author_type="human",
        author_id="baseline",
        target_page_id=baseline_page.id,
    )
    await staged_db.record_annotation(
        annotation_type="flag",
        author_type="human",
        author_id="staged",
        target_page_id=staged_page.id,
    )

    staged_events = await staged_db.get_annotations()
    assert {a.author_id for a in staged_events} == {"baseline", "staged"}

    observer_events = await observer_db.get_annotations()
    assert {a.author_id for a in observer_events} == {"baseline"}
    assert all(a.staged is False for a in observer_events)

    await baseline_db.delete_run_data()
    await staged_db.delete_run_data()
    await observer_db.delete_run_data()


async def test_two_staged_runs_cannot_see_each_other(project_id):
    db_a = await _make_db(project_id, staged=True)
    await _register_run(db_a)
    await db_a.init_budget(100)

    db_b = await _make_db(project_id, staged=True)
    await _register_run(db_b)
    await db_b.init_budget(100)

    page_a = await _make_page(db_a)
    page_b = await _make_page(db_b)

    await db_a.record_annotation(
        annotation_type="span",
        author_type="human",
        author_id="a",
        target_page_id=page_a.id,
    )
    await db_b.record_annotation(
        annotation_type="span",
        author_type="human",
        author_id="b",
        target_page_id=page_b.id,
    )

    events_a = await db_a.get_annotations()
    events_b = await db_b.get_annotations()

    assert [e.author_id for e in events_a] == ["a"]
    assert [e.author_id for e in events_b] == ["b"]

    await db_a.delete_run_data()
    await db_b.delete_run_data()


async def test_annotate_span_move_creates_annotation_event(run_db):
    page = await _make_page(run_db)
    call = await _make_call(run_db)

    state = MoveState(call, run_db)
    tool = MOVES[MoveType.ANNOTATE_SPAN].bind(state)
    await tool.fn(
        {
            "target_page_id": page.id,
            "span_start": 2,
            "span_end": 10,
            "note": "suspicious phrasing",
            "category": "unsupported",
        }
    )

    anns = await run_db.get_annotations(target_page_id=page.id)
    assert len(anns) == 1
    ann = anns[0]
    assert ann.annotation_type == "span"
    assert ann.author_type == "model"
    assert ann.author_id == call.id
    assert ann.span_start == 2
    assert ann.span_end == 10
    assert ann.category == "unsupported"
    assert ann.note == "suspicious phrasing"


async def test_annotate_span_move_records_reputation_event(run_db):
    page = await _make_page(run_db)
    call = await _make_call(run_db)

    state = MoveState(call, run_db)
    tool = MOVES[MoveType.ANNOTATE_SPAN].bind(state)
    await tool.fn(
        {
            "target_page_id": page.id,
            "span_start": 0,
            "span_end": 5,
            "note": "good opener",
            "category": "praise",
        }
    )

    events = await run_db.get_reputation_events(source="model_annotation")
    assert len(events) == 1
    ev = events[0]
    assert ev.dimension == "span"
    assert ev.score == 1.0
    assert ev.source_call_id == call.id
    assert ev.extra["target_page_id"] == page.id
    assert ev.extra["category"] == "praise"


async def test_annotate_alternative_move_creates_annotation_event(run_db):
    call = await _make_call(run_db)

    state = MoveState(call, run_db)
    tool = MOVES[MoveType.ANNOTATE_ALTERNATIVE].bind(state)
    await tool.fn(
        {
            "target_call_id": call.id,
            "target_event_seq": 3,
            "alternative": "fire web_research on the parent question",
            "rationale": "external sources are needed here",
        }
    )

    anns = await run_db.get_annotations(target_call_id=call.id)
    assert len(anns) == 1
    ann = anns[0]
    assert ann.annotation_type == "counterfactual_tool_use"
    assert ann.author_type == "model"
    assert ann.author_id == call.id
    assert ann.target_event_seq == 3
    assert ann.payload["alternative"] == "fire web_research on the parent question"
    assert ann.payload["rationale"] == "external sources are needed here"


async def test_human_annotations_endpoint_creates_row_and_reputation(api_client, tmp_db):
    page = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        headline="Rayleigh scattering",
        content="Sky appears blue due to Rayleigh scattering.",
    )
    await tmp_db.save_page(page)

    resp = await api_client.post(
        "/api/annotations",
        json={
            "annotation_type": "span",
            "target_page_id": page.id,
            "span_start": 4,
            "span_end": 10,
            "category": "factual_error",
            "note": "sky isn't only blue",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["annotation_id"]

    rows = (
        await tmp_db._execute(
            tmp_db.client.table("annotation_events").select("*").eq("target_page_id", page.id)
        )
    ).data
    assert len(rows) == 1
    row = rows[0]
    assert row["annotation_type"] == "span"
    assert row["author_type"] == "human"
    assert row["span_start"] == 4
    assert row["span_end"] == 10
    assert row["category"] == "factual_error"

    rep_rows = (
        await tmp_db._execute(
            tmp_db.client.table("reputation_events")
            .select("*")
            .eq("project_id", tmp_db.project_id)
            .eq("source", "human_feedback")
            .eq("dimension", "span")
        )
    ).data
    assert len(rep_rows) == 1
    assert rep_rows[0]["score"] == 1.0
    assert rep_rows[0]["extra"]["annotation_id"] == body["annotation_id"]

    await tmp_db._execute(
        tmp_db.client.table("annotation_events").delete().eq("id", body["annotation_id"])
    )
    await tmp_db._execute(
        tmp_db.client.table("reputation_events").delete().eq("run_id", rep_rows[0]["run_id"])
    )
    await tmp_db._execute(tmp_db.client.table("runs").delete().eq("id", rep_rows[0]["run_id"]))


async def test_human_annotations_endpoint_rejects_bad_type(api_client, tmp_db):
    resp = await api_client.post(
        "/api/annotations",
        json={"annotation_type": "bogus", "target_page_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 422


async def test_human_annotations_endpoint_404_for_unknown_page(api_client):
    resp = await api_client.post(
        "/api/annotations",
        json={
            "annotation_type": "span",
            "target_page_id": "00000000-0000-0000-0000-000000000000",
            "span_start": 0,
            "span_end": 1,
            "note": "x",
        },
    )
    assert resp.status_code == 404


async def test_list_page_annotations_endpoint(api_client, tmp_db):
    page = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        headline="H",
        content="content",
    )
    await tmp_db.save_page(page)

    post = await api_client.post(
        "/api/annotations",
        json={
            "annotation_type": "endorsement",
            "target_page_id": page.id,
            "note": "nice",
        },
    )
    assert post.status_code == 200
    ann_id = post.json()["annotation_id"]

    resp = await api_client.get(f"/api/pages/{page.id}/annotations")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["annotation_type"] == "endorsement"
    assert body[0]["target_page_id"] == page.id

    rep_rows = (
        await tmp_db._execute(
            tmp_db.client.table("reputation_events")
            .select("run_id")
            .eq("project_id", tmp_db.project_id)
            .eq("source", "human_feedback")
            .eq("dimension", "endorsement")
        )
    ).data
    await tmp_db._execute(tmp_db.client.table("annotation_events").delete().eq("id", ann_id))
    for row in rep_rows:
        await tmp_db._execute(
            tmp_db.client.table("reputation_events").delete().eq("run_id", row["run_id"])
        )
        await tmp_db._execute(tmp_db.client.table("runs").delete().eq("id", row["run_id"]))


async def test_list_call_annotations_endpoint(api_client, tmp_db):
    call = Call(
        call_type=CallType.CLAUDE_CODE_DIRECT,
        workspace=Workspace.RESEARCH,
        status=CallStatus.COMPLETE,
    )
    await tmp_db.save_call(call)

    post = await api_client.post(
        "/api/annotations",
        json={
            "annotation_type": "counterfactual_tool_use",
            "target_call_id": call.id,
            "target_event_seq": 2,
            "note": "alternative X",
            "payload": {"alternative": "X", "rationale": "Y"},
        },
    )
    assert post.status_code == 200
    ann_id = post.json()["annotation_id"]

    resp = await api_client.get(f"/api/calls/{call.id}/annotations")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["target_call_id"] == call.id
    assert body[0]["target_event_seq"] == 2

    await tmp_db._execute(tmp_db.client.table("annotation_events").delete().eq("id", ann_id))
    rep_rows = (
        await tmp_db._execute(
            tmp_db.client.table("reputation_events")
            .select("run_id")
            .eq("source", "human_feedback")
            .eq("dimension", "counterfactual_tool_use")
        )
    ).data
    for row in rep_rows:
        await tmp_db._execute(
            tmp_db.client.table("reputation_events").delete().eq("run_id", row["run_id"])
        )
        await tmp_db._execute(tmp_db.client.table("runs").delete().eq("id", row["run_id"]))


async def test_enable_annotation_moves_appends_to_preset():
    from rumil.available_moves import get_moves_for_call
    from rumil.models import CallType

    with override_settings(enable_annotation_moves=True):
        moves = get_moves_for_call(CallType.FIND_CONSIDERATIONS)
    assert MoveType.ANNOTATE_SPAN in moves
    assert MoveType.ANNOTATE_ALTERNATIVE in moves

    with override_settings(enable_annotation_moves=False):
        moves_off = get_moves_for_call(CallType.FIND_CONSIDERATIONS)
    assert MoveType.ANNOTATE_SPAN not in moves_off
    assert MoveType.ANNOTATE_ALTERNATIVE not in moves_off
