"""Tests for the streaming chat endpoint's client-disconnect resilience.

handle_chat_stream runs the Anthropic turn in a detached asyncio.Task so
a browser disconnect (CancelledError in the SSE generator) does NOT abort
the turn. The task keeps going server-side, persisting the assistant
message and any tool_results. These tests verify that behaviour end-to-end
against the local Supabase DB, with a mocked Anthropic client.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from anthropic.types import TextBlock, TextDelta, ToolUseBlock

from rumil.api import chat as chat_module
from rumil.api.chat import ChatRequest, handle_chat_stream
from rumil.models import ChatMessageRole


@pytest_asyncio.fixture
async def workspace_name(tmp_db):
    return f"chat-stream-shield-{tmp_db.run_id[:8]}"


@pytest_asyncio.fixture
async def project_id(tmp_db, workspace_name):
    project, _ = await tmp_db.get_or_create_project(workspace_name)
    tmp_db.project_id = project.id
    yield project.id
    # handle_chat_stream creates its own DB/run for the project; clean up
    # to avoid FK violations on tmp_db teardown.
    for table in ("chat_messages", "chat_conversations", "calls", "pages", "runs"):
        try:
            await tmp_db._execute(tmp_db.client.table(table).delete().eq("project_id", project.id))
        except Exception:
            pass


def _make_text_delta_event(text: str) -> MagicMock:
    evt = MagicMock()
    evt.type = "content_block_delta"
    evt.delta = TextDelta(type="text_delta", text=text)
    return evt


def _make_final_text_response(text: str) -> MagicMock:
    """Final response with a single assistant text block, no tool calls."""
    resp = MagicMock()
    resp.content = [TextBlock(type="text", text=text, citations=None)]
    usage = MagicMock()
    usage.input_tokens = 10
    usage.output_tokens = 5
    usage.cache_creation_input_tokens = 0
    usage.cache_read_input_tokens = 0
    resp.usage = usage
    return resp


class _FakeStreamCtx:
    """Async context manager returned by client.messages.stream(...).

    Yields a list of pre-baked events, then returns a final_message.
    Optionally blocks in the event loop before yielding each event so we
    can observe cancel semantics at a known point.
    """

    def __init__(
        self,
        events: list[Any],
        final_message: Any,
        pre_event_sleep: float = 0.0,
        block_forever_after: int | None = None,
    ):
        self._events = events
        self._final = final_message
        self._pre = pre_event_sleep
        self._block_after = block_forever_after

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def __aiter__(self):
        for i, evt in enumerate(self._events):
            if self._pre:
                await asyncio.sleep(self._pre)
            if self._block_after is not None and i >= self._block_after:
                # Simulate an Anthropic stream that hangs mid-flight.
                await asyncio.sleep(3600)
            yield evt

    async def get_final_message(self):
        return self._final


def _install_fake_anthropic(
    mocker,
    stream_ctx: _FakeStreamCtx | list[_FakeStreamCtx],
) -> MagicMock:
    """Patch anthropic.AsyncAnthropic so messages.stream returns our fake."""
    fake_client = MagicMock()
    ctxs = stream_ctx if isinstance(stream_ctx, list) else [stream_ctx]
    it = iter(ctxs)
    fake_client.messages.stream = lambda **_kw: next(it)
    mocker.patch.object(
        chat_module.anthropic,
        "AsyncAnthropic",
        return_value=fake_client,
    )
    return fake_client


async def _wait_for_assistant_message(tmp_db, conv_id: str, timeout_s: float = 5.0):
    """Poll list_chat_messages until an ASSISTANT row appears."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        msgs = await tmp_db.list_chat_messages(conv_id)
        for m in msgs:
            if m.role == ChatMessageRole.ASSISTANT:
                return msgs
        await asyncio.sleep(0.05)
    raise AssertionError(f"assistant message never persisted for conv={conv_id}")


async def test_stream_persists_assistant_message_on_normal_completion(
    tmp_db, project_id, workspace_name, mocker
):
    """Baseline: a full SSE stream completes and the assistant row is saved."""
    stream = _FakeStreamCtx(
        events=[
            _make_text_delta_event("Hello "),
            _make_text_delta_event("world"),
        ],
        final_message=_make_final_text_response("Hello world"),
    )
    _install_fake_anthropic(mocker, stream)
    mocker.patch(
        "rumil.api.chat.build_chat_context",
        new=AsyncMock(return_value="stub context"),
    )

    request = ChatRequest(
        question_id="",
        messages=[{"role": "user", "content": "hi"}],
        workspace=workspace_name,
    )
    response = await handle_chat_stream(request)

    # Drain the SSE body normally.
    body_parts: list[str] = []
    async for chunk in response.body_iterator:
        if isinstance(chunk, str):
            body_parts.append(chunk)
        else:
            body_parts.append(bytes(chunk).decode())
    body = "".join(body_parts)

    assert "Hello " in body
    assert "world" in body
    assert "event: done" in body

    # Find the conversation created by the stream (most recent for project).
    convs = await tmp_db.list_chat_conversations(project_id=project_id)
    assert convs, "no conversation was created"
    conv_id = convs[0].id

    msgs = await _wait_for_assistant_message(tmp_db, conv_id)
    assistant = next(m for m in msgs if m.role == ChatMessageRole.ASSISTANT)
    blocks = assistant.content["blocks"]
    assert any(b.get("type") == "text" and b.get("text") == "Hello world" for b in blocks)


async def test_assistant_message_persisted_when_client_disconnects_mid_stream(
    tmp_db, project_id, workspace_name, mocker
):
    """The core shield guarantee: even if the SSE consumer gives up before
    'done', the background turn task finishes and persists the assistant
    row to the DB. Next page load will see the completed turn.
    """
    stream = _FakeStreamCtx(
        events=[
            _make_text_delta_event("partial "),
            _make_text_delta_event("then "),
            _make_text_delta_event("full"),
        ],
        final_message=_make_final_text_response("partial then full"),
        # Small pre-event delay so we can disconnect after the first token.
        pre_event_sleep=0.02,
    )
    _install_fake_anthropic(mocker, stream)
    mocker.patch(
        "rumil.api.chat.build_chat_context",
        new=AsyncMock(return_value="stub context"),
    )

    request = ChatRequest(
        question_id="",
        messages=[{"role": "user", "content": "disconnect me"}],
        workspace=workspace_name,
    )
    response = await handle_chat_stream(request)

    # Consume one chunk, then aclose() to simulate a browser disconnect.
    # FastAPI-side this manifests as the generator being closed/cancelled.
    body_iter = response.body_iterator
    first = await body_iter.__anext__()  # type: ignore[attr-defined]
    assert first  # we got SOMETHING before bailing
    await body_iter.aclose()  # type: ignore[attr-defined]

    # Now the outer generator is dead. But the background turn_task should
    # still be running (or already done). Poll the DB until the assistant
    # row lands.
    convs = await tmp_db.list_chat_conversations(project_id=project_id)
    assert convs, "no conversation was created"
    conv_id = convs[0].id

    msgs = await _wait_for_assistant_message(tmp_db, conv_id, timeout_s=5.0)
    assistant = next(m for m in msgs if m.role == ChatMessageRole.ASSISTANT)
    blocks = assistant.content["blocks"]
    # Final saved content reflects the COMPLETE message, not just the first
    # token we consumed before disconnecting.
    assert any(b.get("type") == "text" and "partial then full" in b.get("text", "") for b in blocks)

    # And the user turn was already persisted at the start.
    user_msgs = [m for m in msgs if m.role == ChatMessageRole.USER]
    assert user_msgs and user_msgs[-1].content["text"] == "disconnect me"


async def test_client_disconnect_does_not_leak_cancelled_error_to_caller(
    tmp_db, project_id, workspace_name, mocker
):
    """aclose() on the SSE body should not raise CancelledError out of
    handle_chat_stream's consumer. The generator swallows it cleanly.
    """
    stream = _FakeStreamCtx(
        events=[_make_text_delta_event("ok")],
        final_message=_make_final_text_response("ok"),
        pre_event_sleep=0.01,
    )
    _install_fake_anthropic(mocker, stream)
    mocker.patch(
        "rumil.api.chat.build_chat_context",
        new=AsyncMock(return_value="stub context"),
    )

    request = ChatRequest(
        question_id="",
        messages=[{"role": "user", "content": "clean disconnect"}],
        workspace=workspace_name,
    )
    response = await handle_chat_stream(request)

    body_iter = response.body_iterator
    await body_iter.__anext__()  # type: ignore[attr-defined]

    # aclose() raises GeneratorExit inside the generator; it should NOT
    # propagate CancelledError to us.
    try:
        await body_iter.aclose()  # type: ignore[attr-defined]
    except asyncio.CancelledError as e:
        pytest.fail(f"CancelledError leaked from aclose: {e}")

    # Let the background task finish so we don't leave it dangling.
    convs = await tmp_db.list_chat_conversations(project_id=project_id)
    assert convs
    await _wait_for_assistant_message(tmp_db, convs[0].id, timeout_s=5.0)


async def test_background_task_survives_generator_gc(tmp_db, project_id, workspace_name, mocker):
    """The turn task is held in a module-level strong-ref set, so even if
    the SSE generator is fully released the task keeps running until it
    finishes and then self-removes from the set.
    """
    stream = _FakeStreamCtx(
        events=[_make_text_delta_event("keepalive")],
        final_message=_make_final_text_response("keepalive"),
        pre_event_sleep=0.05,
    )
    _install_fake_anthropic(mocker, stream)
    mocker.patch(
        "rumil.api.chat.build_chat_context",
        new=AsyncMock(return_value="stub context"),
    )

    request = ChatRequest(
        question_id="",
        messages=[{"role": "user", "content": "gc-test"}],
        workspace=workspace_name,
    )
    before = len(chat_module._live_chat_turns)
    response = await handle_chat_stream(request)

    # Registering the task must happen synchronously inside handle_chat_stream.
    assert len(chat_module._live_chat_turns) == before + 1

    body_iter = response.body_iterator
    await body_iter.aclose()  # type: ignore[attr-defined]
    del response
    del body_iter

    # Poll for the persisted row — the task keeps running even though
    # the response/body_iter are gone.
    convs = await tmp_db.list_chat_conversations(project_id=project_id)
    assert convs
    await _wait_for_assistant_message(tmp_db, convs[0].id, timeout_s=5.0)

    # After completion the task self-removes from the live set.
    deadline = asyncio.get_event_loop().time() + 2.0
    while asyncio.get_event_loop().time() < deadline:
        if len(chat_module._live_chat_turns) == before:
            break
        await asyncio.sleep(0.05)
    assert len(chat_module._live_chat_turns) == before, (
        "turn task did not self-remove from _live_chat_turns after completing"
    )
