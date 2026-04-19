"""Tests for chat conversation persistence.

Covers the new chat_conversations/chat_messages tables, their DB helpers,
the CRUD API endpoints, and the conversation-threading behaviour of
handle_chat. All Anthropic API access is mocked — no real LLM calls.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from rumil.api import chat as chat_module
from rumil.api.app import app
from rumil.api.chat import ChatRequest, handle_chat
from rumil.models import ChatMessageRole


@pytest_asyncio.fixture
async def project_id(tmp_db):
    project, _ = await tmp_db.get_or_create_project("chat-persist-test")
    tmp_db.project_id = project.id
    return project.id


@pytest_asyncio.fixture
async def conversation(tmp_db, project_id):
    return await tmp_db.create_chat_conversation(
        project_id=project_id,
        question_id=None,
        title="Initial title",
    )


async def test_create_conversation_persists_row(tmp_db, project_id):
    conv = await tmp_db.create_chat_conversation(
        project_id=project_id,
        question_id="q-123",
        title="What if?",
    )

    fetched = await tmp_db.get_chat_conversation(conv.id)
    assert fetched is not None
    assert fetched.id == conv.id
    assert fetched.project_id == project_id
    assert fetched.question_id == "q-123"
    assert fetched.title == "What if?"
    assert fetched.deleted_at is None


async def test_save_chat_message_assigns_monotonic_seq(tmp_db, conversation):
    m1 = await tmp_db.save_chat_message(
        conversation_id=conversation.id,
        role=ChatMessageRole.USER,
        content={"text": "hello"},
    )
    m2 = await tmp_db.save_chat_message(
        conversation_id=conversation.id,
        role=ChatMessageRole.ASSISTANT,
        content={"blocks": [{"type": "text", "text": "hi"}]},
    )
    m3 = await tmp_db.save_chat_message(
        conversation_id=conversation.id,
        role=ChatMessageRole.USER,
        content={"text": "follow-up"},
    )

    assert m1.seq == 0
    assert m2.seq == 1
    assert m3.seq == 2

    listed = await tmp_db.list_chat_messages(conversation.id)
    assert [m.seq for m in listed] == [0, 1, 2]
    assert [m.role for m in listed] == [
        ChatMessageRole.USER,
        ChatMessageRole.ASSISTANT,
        ChatMessageRole.USER,
    ]
    assert listed[0].content == {"text": "hello"}
    assert listed[2].content == {"text": "follow-up"}


async def test_list_chat_conversations_orders_by_updated_at_desc(tmp_db, project_id):
    c1 = await tmp_db.create_chat_conversation(project_id, title="first")
    c2 = await tmp_db.create_chat_conversation(project_id, title="second")
    c3 = await tmp_db.create_chat_conversation(project_id, title="third")

    await tmp_db.update_chat_conversation(c1.id, title="first updated")

    listed = await tmp_db.list_chat_conversations(project_id=project_id)
    ids_in_order = [c.id for c in listed]

    assert c1.id in ids_in_order
    assert c2.id in ids_in_order
    assert c3.id in ids_in_order
    assert ids_in_order[0] == c1.id


async def test_list_chat_conversations_filters_by_question_id(tmp_db, project_id):
    c_a = await tmp_db.create_chat_conversation(
        project_id=project_id, question_id="q-alpha", title="alpha chat"
    )
    c_b = await tmp_db.create_chat_conversation(
        project_id=project_id, question_id="q-beta", title="beta chat"
    )

    alpha_only = await tmp_db.list_chat_conversations(project_id=project_id, question_id="q-alpha")
    assert [c.id for c in alpha_only] == [c_a.id]

    beta_only = await tmp_db.list_chat_conversations(project_id=project_id, question_id="q-beta")
    assert [c.id for c in beta_only] == [c_b.id]

    no_filter = await tmp_db.list_chat_conversations(project_id=project_id)
    no_filter_ids = {c.id for c in no_filter}
    assert c_a.id in no_filter_ids
    assert c_b.id in no_filter_ids


async def test_soft_delete_hides_conversation_from_list_and_get(tmp_db, conversation):
    await tmp_db.soft_delete_chat_conversation(conversation.id)

    fetched = await tmp_db.get_chat_conversation(conversation.id)
    assert fetched is None

    listed = await tmp_db.list_chat_conversations(project_id=conversation.project_id)
    assert all(c.id != conversation.id for c in listed)


async def test_update_chat_conversation_changes_title(tmp_db, conversation):
    await tmp_db.update_chat_conversation(conversation.id, title="Renamed")
    refreshed = await tmp_db.get_chat_conversation(conversation.id)
    assert refreshed is not None
    assert refreshed.title == "Renamed"


def _tc():
    return TestClient(app)


async def test_api_create_and_list_conversations(tmp_db, project_id):
    with _tc() as client:
        resp = client.post(
            "/api/chat/conversations",
            json={
                "project_id": project_id,
                "question_id": "q-abc",
                "first_message": "What about markets?",
            },
        )
        assert resp.status_code == 200, resp.text
        created = resp.json()
        assert created["project_id"] == project_id
        assert created["question_id"] == "q-abc"
        assert "markets" in created["title"].lower()

        resp2 = client.get(f"/api/chat/conversations?project_id={project_id}")
        assert resp2.status_code == 200
        listed = resp2.json()
        assert any(c["id"] == created["id"] for c in listed)


async def test_api_get_conversation_includes_messages(tmp_db, project_id):
    with _tc() as client:
        resp = client.post(
            "/api/chat/conversations",
            json={
                "project_id": project_id,
                "first_message": "hello world",
            },
        )
        cid = resp.json()["id"]

        detail = client.get(f"/api/chat/conversations/{cid}").json()
        assert detail["id"] == cid
        assert detail["title"].startswith("hello")
        assert len(detail["messages"]) == 1
        assert detail["messages"][0]["role"] == "user"
        assert detail["messages"][0]["content"]["text"] == "hello world"


async def test_api_rename_conversation(tmp_db, project_id):
    with _tc() as client:
        create = client.post(
            "/api/chat/conversations",
            json={"project_id": project_id, "first_message": "original"},
        )
        cid = create.json()["id"]

        patch = client.patch(
            f"/api/chat/conversations/{cid}",
            json={"title": "Brand new title"},
        )
        assert patch.status_code == 200
        assert patch.json()["title"] == "Brand new title"

        detail = client.get(f"/api/chat/conversations/{cid}").json()
        assert detail["title"] == "Brand new title"


async def test_api_soft_delete_conversation(tmp_db, project_id):
    with _tc() as client:
        create = client.post(
            "/api/chat/conversations",
            json={"project_id": project_id, "first_message": "doomed"},
        )
        cid = create.json()["id"]

        deleted = client.delete(f"/api/chat/conversations/{cid}")
        assert deleted.status_code == 200

        not_found = client.get(f"/api/chat/conversations/{cid}")
        assert not_found.status_code == 404

        listed = client.get(f"/api/chat/conversations?project_id={project_id}").json()
        assert all(c["id"] != cid for c in listed)


async def test_api_rename_unknown_id_returns_404(tmp_db):
    with _tc() as client:
        resp = client.patch(
            "/api/chat/conversations/00000000-0000-0000-0000-000000000000",
            json={"title": "ghost"},
        )
        assert resp.status_code == 404


def _fake_anthropic_response(text: str):
    """Build a fake Anthropic response whose content is real TextBlock objects."""
    from anthropic.types import TextBlock

    block = TextBlock(type="text", text=text, citations=None)
    msg = MagicMock()
    msg.content = [block]
    return msg


async def test_handle_chat_auto_creates_conversation_when_missing(tmp_db, project_id, mocker):
    """Calling handle_chat with no conversation_id auto-creates one and persists messages."""
    fake_response = _fake_anthropic_response("Reply from assistant")

    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(return_value=fake_response)
    mocker.patch.object(
        chat_module.anthropic,
        "AsyncAnthropic",
        return_value=fake_client,
    )
    mocker.patch(
        "rumil.api.chat.build_chat_context",
        new=AsyncMock(return_value="stub context"),
    )
    request = ChatRequest(
        question_id="",
        messages=[{"role": "user", "content": "What do we know?"}],
        workspace="chat-persist-test",
    )
    response = await handle_chat(request)

    assert response.response == "Reply from assistant"
    assert response.conversation_id

    conv = await tmp_db.get_chat_conversation(response.conversation_id)
    assert conv is not None
    assert "know" in conv.title.lower()

    messages = await tmp_db.list_chat_messages(response.conversation_id)
    roles = [m.role for m in messages]
    assert ChatMessageRole.USER in roles
    assert ChatMessageRole.ASSISTANT in roles

    user_msg = next(m for m in messages if m.role == ChatMessageRole.USER)
    assert user_msg.content["text"] == "What do we know?"

    asst_msg = next(m for m in messages if m.role == ChatMessageRole.ASSISTANT)
    assert asst_msg.content["blocks"][0]["type"] == "text"
    assert asst_msg.content["blocks"][0]["text"] == "Reply from assistant"


async def test_handle_chat_resumes_existing_conversation(tmp_db, project_id, mocker):
    """Passing conversation_id loads prior messages and persists new turn."""
    conv = await tmp_db.create_chat_conversation(
        project_id=project_id,
        question_id=None,
        title="Existing",
    )
    await tmp_db.save_chat_message(
        conversation_id=conv.id,
        role=ChatMessageRole.USER,
        content={"text": "first question"},
    )
    await tmp_db.save_chat_message(
        conversation_id=conv.id,
        role=ChatMessageRole.ASSISTANT,
        content={"blocks": [{"type": "text", "text": "first answer"}]},
    )

    fake_response = _fake_anthropic_response("second answer")
    fake_client = MagicMock()

    captured: dict[str, Any] = {}

    async def capture_create(**kwargs):
        captured["messages"] = kwargs["messages"]
        return fake_response

    fake_client.messages.create = capture_create
    mocker.patch.object(
        chat_module.anthropic,
        "AsyncAnthropic",
        return_value=fake_client,
    )
    mocker.patch(
        "rumil.api.chat.build_chat_context",
        new=AsyncMock(return_value="stub context"),
    )
    request = ChatRequest(
        question_id="",
        messages=[
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "first answer"},
            {"role": "user", "content": "second question"},
        ],
        workspace="chat-persist-test",
        conversation_id=conv.id,
    )
    response = await handle_chat(request)

    assert response.conversation_id == conv.id

    sent = captured["messages"]
    assert len(sent) >= 2
    assert any(
        (m.get("role") == "user" and "first question" in _extract_text(m.get("content")))
        for m in sent
    )
    assert any(
        (m.get("role") == "user" and "second question" in _extract_text(m.get("content")))
        for m in sent
    )

    messages = await tmp_db.list_chat_messages(conv.id)
    user_texts = [m.content.get("text") for m in messages if m.role == ChatMessageRole.USER]
    assert "first question" in user_texts
    assert "second question" in user_texts

    assistant_texts: list[str] = []
    for m in messages:
        if m.role == ChatMessageRole.ASSISTANT:
            for b in m.content.get("blocks", []):
                if b.get("type") == "text":
                    assistant_texts.append(b["text"])
    assert "first answer" in assistant_texts
    assert "second answer" in assistant_texts


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(str(b.get("text", "")))
        return " ".join(parts)
    return ""


async def test_derive_title_truncates_long_message():
    from rumil.api.chat import _derive_title

    long = "This is a fairly long first message " * 10
    title = _derive_title(long)
    assert len(title) <= 80
    assert title.endswith("...")

    short = "short"
    assert _derive_title(short) == "short"
