"""Chat conversation + message store: create → append → branch → list."""

import pytest

from rumil.models import ChatMessageRole


@pytest.mark.asyncio
async def test_chat_conversation_lifecycle(tmp_db):
    conv = await tmp_db.create_chat_conversation(
        project_id=tmp_db.project_id,
        question_id=None,
        title="original",
    )

    m0 = await tmp_db.save_chat_message(conv.id, ChatMessageRole.USER, {"text": "first"})
    m1 = await tmp_db.save_chat_message(conv.id, ChatMessageRole.ASSISTANT, {"text": "reply"})
    m2 = await tmp_db.save_chat_message(conv.id, ChatMessageRole.USER, {"text": "followup"})
    assert [m0.seq, m1.seq, m2.seq] == [0, 1, 2]

    messages = await tmp_db.list_chat_messages(conv.id)
    assert [m.seq for m in messages] == [0, 1, 2]
    assert [m.role for m in messages] == [
        ChatMessageRole.USER,
        ChatMessageRole.ASSISTANT,
        ChatMessageRole.USER,
    ]

    branch = await tmp_db.branch_chat_conversation(conv.id, at_seq=1)
    assert branch.parent_conversation_id == conv.id
    assert branch.branched_at_seq == 1
    branch_messages = await tmp_db.list_chat_messages(branch.id)
    assert [m.seq for m in branch_messages] == [0, 1]
    assert [m.content["text"] for m in branch_messages] == ["first", "reply"]

    await tmp_db.save_chat_message(branch.id, ChatMessageRole.USER, {"text": "new direction"})
    branch_messages = await tmp_db.list_chat_messages(branch.id)
    assert [m.seq for m in branch_messages] == [0, 1, 2]
    assert branch_messages[-1].content["text"] == "new direction"

    source_messages = await tmp_db.list_chat_messages(conv.id)
    assert len(source_messages) == 3

    convs = await tmp_db.list_chat_conversations(tmp_db.project_id)
    assert {c.id for c in convs} == {conv.id, branch.id}

    await tmp_db.update_chat_conversation(conv.id, title="renamed")
    reloaded = await tmp_db.get_chat_conversation(conv.id)
    assert reloaded is not None
    assert reloaded.title == "renamed"

    await tmp_db.soft_delete_chat_conversation(conv.id)
    assert await tmp_db.get_chat_conversation(conv.id) is None
    convs_after = await tmp_db.list_chat_conversations(tmp_db.project_id)
    assert {c.id for c in convs_after} == {branch.id}


@pytest.mark.asyncio
async def test_branch_rejects_invalid_seq(tmp_db):
    conv = await tmp_db.create_chat_conversation(project_id=tmp_db.project_id)
    await tmp_db.save_chat_message(conv.id, ChatMessageRole.USER, {"text": "a"})
    await tmp_db.save_chat_message(conv.id, ChatMessageRole.ASSISTANT, {"text": "b"})

    with pytest.raises(ValueError):
        await tmp_db.branch_chat_conversation(conv.id, at_seq=-1)
    with pytest.raises(ValueError):
        await tmp_db.branch_chat_conversation(conv.id, at_seq=99)

    empty = await tmp_db.create_chat_conversation(project_id=tmp_db.project_id)
    with pytest.raises(ValueError):
        await tmp_db.branch_chat_conversation(empty.id, at_seq=0)
