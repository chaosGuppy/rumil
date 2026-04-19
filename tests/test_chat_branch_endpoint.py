"""Tests for the chat conversation branching feature.

Covers the DB helper (`branch_chat_conversation`) and the HTTP endpoint
(`POST /api/chat/conversations/{id}/branch`). No LLM calls — this is
pure persistence + API wiring.

Branching semantics: copy all messages where seq <= at_seq from the
source conversation into a new conversation, preserve their content
and question_id, link the new convo via parent_conversation_id +
branched_at_seq. The source is untouched.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from rumil.api.app import app
from rumil.models import ChatMessageRole


@pytest_asyncio.fixture
async def workspace_name(tmp_db):
    return f"chat-branch-{tmp_db.run_id[:8]}"


@pytest_asyncio.fixture
async def project_id(tmp_db, workspace_name):
    project, _ = await tmp_db.get_or_create_project(workspace_name)
    tmp_db.project_id = project.id
    yield project.id
    # Mirror test_chat_persistence.py teardown: clean up rows that the
    # shared project FKs into, so tmp_db.delete_project=True doesn't
    # fail on dangling refs from test-created runs/calls/pages.
    for table in ("calls", "pages", "runs"):
        await tmp_db._execute(tmp_db.client.table(table).delete().eq("project_id", project.id))


@pytest_asyncio.fixture
async def seeded_conversation(tmp_db, project_id):
    """A conversation with 4 messages at seqs 0..3, alternating roles."""
    conv = await tmp_db.create_chat_conversation(
        project_id=project_id,
        question_id="q-root",
        title="Main line",
    )
    await tmp_db.save_chat_message(
        conversation_id=conv.id,
        role=ChatMessageRole.USER,
        content={"text": "msg 0"},
        question_id="q-root",
    )
    await tmp_db.save_chat_message(
        conversation_id=conv.id,
        role=ChatMessageRole.ASSISTANT,
        content={"blocks": [{"type": "text", "text": "reply 1"}]},
        question_id="q-root",
    )
    await tmp_db.save_chat_message(
        conversation_id=conv.id,
        role=ChatMessageRole.USER,
        content={"text": "msg 2"},
        question_id="q-other",
    )
    await tmp_db.save_chat_message(
        conversation_id=conv.id,
        role=ChatMessageRole.ASSISTANT,
        content={"blocks": [{"type": "text", "text": "reply 3"}]},
        question_id="q-other",
    )
    return conv


async def test_branch_copies_messages_up_to_seq(tmp_db, seeded_conversation):
    new_conv = await tmp_db.branch_chat_conversation(
        source_conversation_id=seeded_conversation.id,
        at_seq=1,
    )

    assert new_conv.id != seeded_conversation.id
    assert new_conv.project_id == seeded_conversation.project_id
    assert new_conv.question_id == seeded_conversation.question_id
    assert new_conv.parent_conversation_id == seeded_conversation.id
    assert new_conv.branched_at_seq == 1

    copied = await tmp_db.list_chat_messages(new_conv.id)
    assert len(copied) == 2
    assert [m.seq for m in copied] == [0, 1]
    assert [m.role for m in copied] == [
        ChatMessageRole.USER,
        ChatMessageRole.ASSISTANT,
    ]
    assert copied[0].content == {"text": "msg 0"}
    assert copied[1].content == {"blocks": [{"type": "text", "text": "reply 1"}]}
    assert copied[0].question_id == "q-root"


async def test_branch_preserves_per_message_question_id(tmp_db, seeded_conversation):
    new_conv = await tmp_db.branch_chat_conversation(
        source_conversation_id=seeded_conversation.id,
        at_seq=3,
    )
    copied = await tmp_db.list_chat_messages(new_conv.id)
    question_ids = [m.question_id for m in copied]
    assert question_ids == ["q-root", "q-root", "q-other", "q-other"]


async def test_branch_at_seq_zero_copies_only_first_message(tmp_db, seeded_conversation):
    new_conv = await tmp_db.branch_chat_conversation(
        source_conversation_id=seeded_conversation.id,
        at_seq=0,
    )
    copied = await tmp_db.list_chat_messages(new_conv.id)
    assert len(copied) == 1
    assert copied[0].seq == 0
    assert copied[0].content == {"text": "msg 0"}
    assert new_conv.branched_at_seq == 0


async def test_branch_leaves_parent_unchanged(tmp_db, seeded_conversation):
    before = await tmp_db.list_chat_messages(seeded_conversation.id)
    await tmp_db.branch_chat_conversation(
        source_conversation_id=seeded_conversation.id,
        at_seq=1,
    )
    after = await tmp_db.list_chat_messages(seeded_conversation.id)
    assert len(after) == len(before) == 4
    assert [m.id for m in after] == [m.id for m in before]
    assert [m.content for m in after] == [m.content for m in before]

    parent = await tmp_db.get_chat_conversation(seeded_conversation.id)
    assert parent is not None
    assert parent.parent_conversation_id is None
    assert parent.branched_at_seq is None
    assert parent.title == seeded_conversation.title


async def test_branch_new_ids_are_independent(tmp_db, seeded_conversation):
    """Editing a copied message in the branch must not affect the source."""
    new_conv = await tmp_db.branch_chat_conversation(
        source_conversation_id=seeded_conversation.id,
        at_seq=2,
    )
    original_ids = {m.id for m in await tmp_db.list_chat_messages(seeded_conversation.id)}
    copied_ids = {m.id for m in await tmp_db.list_chat_messages(new_conv.id)}
    assert original_ids.isdisjoint(copied_ids)


async def test_branch_auto_title(tmp_db, seeded_conversation):
    new_conv = await tmp_db.branch_chat_conversation(
        source_conversation_id=seeded_conversation.id,
        at_seq=2,
    )
    assert "Main line" in new_conv.title
    assert "msg 2" in new_conv.title


async def test_branch_title_override(tmp_db, seeded_conversation):
    new_conv = await tmp_db.branch_chat_conversation(
        source_conversation_id=seeded_conversation.id,
        at_seq=1,
        title="Alternative path",
    )
    assert new_conv.title == "Alternative path"


async def test_branch_unknown_source_raises(tmp_db):
    with pytest.raises(ValueError, match="not found"):
        await tmp_db.branch_chat_conversation(
            source_conversation_id="00000000-0000-0000-0000-000000000000",
            at_seq=0,
        )


async def test_branch_negative_seq_raises(tmp_db, seeded_conversation):
    with pytest.raises(ValueError, match=">= 0"):
        await tmp_db.branch_chat_conversation(
            source_conversation_id=seeded_conversation.id,
            at_seq=-1,
        )


async def test_branch_seq_beyond_last_message_raises(tmp_db, project_id):
    """Branching at a seq that no message reaches should raise.

    Tested on a conversation with zero messages — any non-negative
    at_seq falls beyond the last message.
    """
    empty = await tmp_db.create_chat_conversation(project_id=project_id, title="empty")
    with pytest.raises(ValueError, match="does not correspond"):
        await tmp_db.branch_chat_conversation(
            source_conversation_id=empty.id,
            at_seq=0,
        )


def _tc():
    return TestClient(app)


async def test_api_branch_happy_path(tmp_db, seeded_conversation):
    with _tc() as client:
        resp = client.post(
            f"/api/chat/conversations/{seeded_conversation.id}/branch",
            json={"at_seq": 1},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()

        assert body["id"] != seeded_conversation.id
        assert body["parent_conversation_id"] == seeded_conversation.id
        assert body["branched_at_seq"] == 1
        assert body["project_id"] == seeded_conversation.project_id
        assert body["question_id"] == seeded_conversation.question_id

        msgs = body["messages"]
        assert len(msgs) == 2
        assert [m["seq"] for m in msgs] == [0, 1]
        assert msgs[0]["role"] == "user"
        assert msgs[0]["content"] == {"text": "msg 0"}
        assert msgs[1]["role"] == "assistant"


async def test_api_branch_at_seq_zero(tmp_db, seeded_conversation):
    with _tc() as client:
        resp = client.post(
            f"/api/chat/conversations/{seeded_conversation.id}/branch",
            json={"at_seq": 0},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["messages"]) == 1
        assert body["messages"][0]["content"] == {"text": "msg 0"}
        assert body["branched_at_seq"] == 0


async def test_api_branch_with_title_override(tmp_db, seeded_conversation):
    with _tc() as client:
        resp = client.post(
            f"/api/chat/conversations/{seeded_conversation.id}/branch",
            json={"at_seq": 1, "title": "Detour"},
        )
        assert resp.status_code == 200
        assert resp.json()["title"] == "Detour"


async def test_api_branch_beyond_last_message_returns_400(tmp_db, seeded_conversation):
    with _tc() as client:
        resp = client.post(
            f"/api/chat/conversations/{seeded_conversation.id}/branch",
            json={"at_seq": 999},
        )
        assert resp.status_code == 400
        assert "does not correspond" in resp.json()["detail"]


async def test_api_branch_unknown_conversation_returns_404(tmp_db):
    with _tc() as client:
        resp = client.post(
            "/api/chat/conversations/00000000-0000-0000-0000-000000000000/branch",
            json={"at_seq": 0},
        )
        assert resp.status_code == 404


async def test_api_branch_negative_seq_returns_400(tmp_db, seeded_conversation):
    with _tc() as client:
        resp = client.post(
            f"/api/chat/conversations/{seeded_conversation.id}/branch",
            json={"at_seq": -1},
        )
        assert resp.status_code == 400


async def test_api_list_conversations_includes_branch_metadata(tmp_db, seeded_conversation):
    """After a branch, listing conversations should surface branch fields so
    the sidebar can render a ↪ marker without a second round-trip."""
    with _tc() as client:
        branch = client.post(
            f"/api/chat/conversations/{seeded_conversation.id}/branch",
            json={"at_seq": 1},
        ).json()

        listed = client.get(
            f"/api/chat/conversations?project_id={seeded_conversation.project_id}"
        ).json()

        branch_entry = next(c for c in listed if c["id"] == branch["id"])
        assert branch_entry["parent_conversation_id"] == seeded_conversation.id
        assert branch_entry["branched_at_seq"] == 1

        original_entry = next(c for c in listed if c["id"] == seeded_conversation.id)
        assert original_entry["parent_conversation_id"] is None
        assert original_entry["branched_at_seq"] is None
