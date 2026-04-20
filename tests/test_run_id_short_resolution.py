"""Tests for short-run-id tolerance across /api/runs/{run_id}/* endpoints.

Covers three surfaces:

1. ``DB.resolve_run_id`` — accepts full UUIDs, 8-char prefixes, returns
   None for empty/unknown.
2. The run-scoped REST endpoints (``trace-tree``, ``spend``, ``nudges``
   GET + POST, ``alerts``) accept an 8-char short id and produce the
   same response as the full UUID.
3. ``RunStore.get_active_chat_dispatches`` and the
   ``list_running_dispatches`` chat tool fall back to the DB when the
   in-memory ``_live_runs_by_conv`` dict is empty (the bug symptom that
   triggered this series — a process restart wiped the dict while the
   dispatched call was still running).
"""

from __future__ import annotations

import json
import uuid

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from rumil.api.app import app
from rumil.api.chat import _execute_tool, _live_runs_by_conv
from rumil.database import DB
from rumil.models import (
    Call,
    CallStatus,
    CallType,
    ChatMessageRole,
    Page,
    PageLayer,
    PageType,
    Workspace,
)


@pytest_asyncio.fixture
async def api_client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest_asyncio.fixture
async def question(tmp_db):
    page = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Does short-id resolution work end-to-end?",
        headline="Does short-id resolution work end-to-end?",
    )
    await tmp_db.save_page(page)
    return page


@pytest_asyncio.fixture
async def run_with_root_call(tmp_db, question):
    await tmp_db.create_run(name="short-id-test", question_id=question.id, config={})
    call = Call(
        call_type=CallType.ASSESS,
        workspace=Workspace.RESEARCH,
        scope_page_id=question.id,
        status=CallStatus.COMPLETE,
        cost_usd=0.01,
    )
    await tmp_db.save_call(call)
    return {"run_id": tmp_db.run_id, "call": call, "question": question}


async def test_resolve_run_id_accepts_full_uuid(tmp_db, run_with_root_call):
    full = run_with_root_call["run_id"]
    assert await tmp_db.resolve_run_id(full) == full


async def test_resolve_run_id_accepts_short_prefix(tmp_db, run_with_root_call):
    full = run_with_root_call["run_id"]
    assert await tmp_db.resolve_run_id(full[:8]) == full


async def test_resolve_run_id_returns_none_for_empty(tmp_db):
    assert await tmp_db.resolve_run_id("") is None


async def test_resolve_run_id_returns_none_for_unknown(tmp_db):
    assert await tmp_db.resolve_run_id("ffffffff") is None


async def test_resolve_run_id_project_scoped(tmp_db, run_with_root_call):
    full = run_with_root_call["run_id"]
    assert await tmp_db.resolve_run_id(full[:8], project_id=tmp_db.project_id) == full
    assert await tmp_db.resolve_run_id(full[:8], project_id=str(uuid.uuid4())) is None


async def test_trace_tree_endpoint_accepts_short_id(api_client, run_with_root_call):
    full = run_with_root_call["run_id"]
    resp_full = await api_client.get(f"/api/runs/{full}/trace-tree")
    resp_short = await api_client.get(f"/api/runs/{full[:8]}/trace-tree")
    assert resp_full.status_code == 200
    assert resp_short.status_code == 200
    assert resp_full.json()["run_id"] == resp_short.json()["run_id"] == full


async def test_trace_tree_endpoint_404_on_unknown(api_client):
    resp = await api_client.get("/api/runs/ffffff00/trace-tree")
    assert resp.status_code == 404


async def test_spend_endpoint_accepts_short_id(api_client, run_with_root_call):
    full = run_with_root_call["run_id"]
    resp_full = await api_client.get(f"/api/runs/{full}/spend")
    resp_short = await api_client.get(f"/api/runs/{full[:8]}/spend")
    assert resp_full.status_code == 200
    assert resp_short.status_code == 200
    assert resp_full.json()["total_calls"] == resp_short.json()["total_calls"] == 1


async def test_alerts_endpoint_accepts_short_id(api_client, run_with_root_call):
    full = run_with_root_call["run_id"]
    resp_full = await api_client.get(f"/api/runs/{full}/alerts")
    resp_short = await api_client.get(f"/api/runs/{full[:8]}/alerts")
    assert resp_full.status_code == 200
    assert resp_short.status_code == 200


async def test_nudges_get_endpoint_accepts_short_id(api_client, run_with_root_call):
    full = run_with_root_call["run_id"]
    resp_full = await api_client.get(f"/api/runs/{full}/nudges")
    resp_short = await api_client.get(f"/api/runs/{full[:8]}/nudges")
    assert resp_full.status_code == 200
    assert resp_short.status_code == 200


async def test_nudges_post_endpoint_accepts_short_id(api_client, run_with_root_call):
    full = run_with_root_call["run_id"]
    body = {
        "kind": "inject_note",
        "durability": "one_shot",
        "soft_text": "test short-id POST",
    }
    resp = await api_client.post(f"/api/runs/{full[:8]}/nudges", json=body)
    assert resp.status_code == 201
    assert resp.json()["run_id"] == full


@pytest_asyncio.fixture
async def chat_dispatch_run(tmp_db, question):
    """A run tagged as if spawned from chat dispatch, with a running root call.

    Mirrors what ``_precreate_run_row`` now writes when chat fires a
    dispatch — ``config.chat`` carries conv_id/tool_use_id/call_type/
    headline so ``get_active_chat_dispatches`` can resolve the run back
    to the conversation. The call is saved via a second DB bound to the
    dispatched run_id so ``save_call`` stamps the right ``run_id``.
    """
    conv = await tmp_db.create_chat_conversation(
        project_id=tmp_db.project_id, question_id=question.id
    )
    new_run_id = str(uuid.uuid4())
    await tmp_db._execute(
        tmp_db.client.table("runs").insert(
            {
                "id": new_run_id,
                "name": "chat dispatch: assess",
                "project_id": tmp_db.project_id,
                "question_id": question.id,
                "config": {
                    "chat": {
                        "conv_id": conv.id,
                        "tool_use_id": "toolu_short_id_test",
                        "call_type": "assess",
                        "headline": question.headline,
                    }
                },
                "staged": False,
            }
        )
    )
    bg_db = await DB.create(run_id=new_run_id)
    bg_db.project_id = tmp_db.project_id
    call = Call(
        call_type=CallType.ASSESS,
        workspace=Workspace.RESEARCH,
        scope_page_id=question.id,
        status=CallStatus.RUNNING,
    )
    try:
        await bg_db.save_call(call)
    finally:
        await bg_db.close()
    return {"conv_id": conv.id, "run_id": new_run_id, "call": call}


async def test_get_active_chat_dispatches_surfaces_tagged_live_run(tmp_db, chat_dispatch_run):
    rows = await tmp_db.runs.get_active_chat_dispatches(chat_dispatch_run["conv_id"])
    assert [r["run_id"] for r in rows] == [chat_dispatch_run["run_id"]]
    entry = rows[0]
    assert entry["call_type"] == "assess"
    assert entry["tool_use_id"] == "toolu_short_id_test"


async def test_get_active_chat_dispatches_hides_terminal_root_call(tmp_db, chat_dispatch_run):
    call = chat_dispatch_run["call"]
    call.status = CallStatus.COMPLETE
    bg_db = await DB.create(run_id=chat_dispatch_run["run_id"])
    bg_db.project_id = tmp_db.project_id
    try:
        await bg_db.save_call(call)
    finally:
        await bg_db.close()
    rows = await tmp_db.runs.get_active_chat_dispatches(chat_dispatch_run["conv_id"])
    assert rows == []


async def test_get_active_chat_dispatches_hides_run_with_dispatch_result(tmp_db, chat_dispatch_run):
    await tmp_db.save_chat_message(
        conversation_id=chat_dispatch_run["conv_id"],
        role=ChatMessageRole.DISPATCH_RESULT,
        content={"run_id": chat_dispatch_run["run_id"], "status": "completed"},
    )
    rows = await tmp_db.runs.get_active_chat_dispatches(chat_dispatch_run["conv_id"])
    assert rows == []


async def test_list_running_dispatches_uses_db_fallback_after_memory_loss(
    tmp_db, chat_dispatch_run
):
    """Reproduces the reported bug: in-memory dict empty but call still live.

    The chat tool should still see the dispatch via the DB-backed view
    so the model can surface it and nudge it.
    """
    _live_runs_by_conv.pop(chat_dispatch_run["conv_id"], None)

    result = await _execute_tool(
        "list_running_dispatches",
        {},
        tmp_db,
        conv_id=chat_dispatch_run["conv_id"],
    )
    payload = json.loads(result)
    assert payload["count"] == 1
    assert payload["runs"][0]["run_id"] == chat_dispatch_run["run_id"]
    assert payload["runs"][0]["call_type"] == "assess"
