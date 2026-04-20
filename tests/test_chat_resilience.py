"""Tests for chat resilience fixes:

1. ``_execute_tool_timed`` logs cancellations with timing and re-raises
   ``CancelledError`` (so slow tools the user gave up on are visible).
2. ``ingest_source`` is background-ified via the ``__async_ingest__``
   sentinel + ``_run_ingest`` handler, mirroring ``dispatch_call`` and
   ``start_research`` — no inline scrape in the streaming response.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest
import pytest_asyncio

from rumil.api.chat import (
    _ASYNC_HANDLERS,
    _await_live_dispatches,
    _execute_tool,
    _execute_tool_timed,
    _run_ingest,
)
from rumil.models import (
    ChatMessageRole,
    Page,
    PageLayer,
    PageType,
    Workspace,
)
from rumil.scraper import ScrapedPage


async def _run_ingest_and_wait(tmp_db, question_id_hint, params):
    """Fire _run_ingest and wait for its bg task to finish.

    Returns (receipt_str, dispatch_result_content_or_None).
    """
    conv = await tmp_db.create_chat_conversation(
        project_id=tmp_db.project_id, question_id=question_id_hint
    )
    receipt = await _run_ingest(
        tmp_db,
        params,
        conv_id=conv.id,
        tool_use_id="toolu_test_ingest",
    )
    await _await_live_dispatches()
    messages = await tmp_db.list_chat_messages(conv.id)
    completions = [m for m in messages if m.role == ChatMessageRole.DISPATCH_RESULT]
    content = completions[-1].content if completions else None
    return receipt, content


@pytest_asyncio.fixture
async def seeded_question(tmp_db):
    """A single root question we can target with ingest extraction."""
    await tmp_db.create_run(name="test-resilience", question_id=None, config={})
    root = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="How does background ingest affect chat resilience?",
        headline="How does background ingest affect chat resilience?",
    )
    await tmp_db.save_page(root)
    return root


async def test_ingest_source_returns_sentinel_without_scraping(tmp_db, seeded_question, mocker):
    """ingest_source in _execute_tool must return a sentinel, NOT scrape inline.

    This is the whole point of the background-ification: scraping blocks on
    network IO, so if we're still hitting it inside the request handler the
    fix didn't land.
    """
    scrape_spy = mocker.patch(
        "rumil.api.chat.scrape_url",
        side_effect=AssertionError("scrape_url must not be called inside _execute_tool"),
    )

    result_str = await _execute_tool(
        "ingest_source",
        {"url": "https://example.com/post", "target_question_id": seeded_question.id[:8]},
        tmp_db,
    )
    result = json.loads(result_str)

    assert result["__async_ingest__"] is True
    assert result["url"] == "https://example.com/post"
    assert result["target_question_id"] == seeded_question.id[:8]
    assert scrape_spy.call_count == 0


async def test_ingest_source_sentinel_is_registered_for_async_dispatch():
    """The sentinel key must be in _ASYNC_HANDLERS — otherwise the streaming
    layer won't know to run it in the background."""
    assert "__async_ingest__" in _ASYNC_HANDLERS
    assert _ASYNC_HANDLERS["__async_ingest__"] is _run_ingest


async def test_run_ingest_scrapes_saves_and_dispatches(tmp_db, seeded_question, mocker):
    """Happy path: receipt returns immediately; bg task scrapes, saves a
    SOURCE page, creates an INGEST call linked to the target question, runs
    the extraction, and writes a completed DISPATCH_RESULT row."""
    scraped = ScrapedPage(
        url="https://example.com/post",
        title="A Useful Post",
        content="This post makes several claims about X.",
        fetched_at="2026-01-01T00:00:00+00:00",
    )
    mocker.patch("rumil.api.chat.scrape_url", return_value=scraped)
    run_spy = mocker.patch(
        "rumil.calls.ingest.IngestCall.run",
        return_value=None,
    )

    receipt, content = await _run_ingest_and_wait(
        tmp_db,
        seeded_question.id,
        {
            "url": "https://example.com/post",
            "target_question_id": seeded_question.id[:8],
            "headline": "A Useful Post",
        },
    )
    assert "started" in receipt.lower()
    assert content is not None
    assert content["status"] == "completed"
    assert "Created source page" in content["summary"]

    sources = await tmp_db.get_pages(page_type=PageType.SOURCE)
    assert len(sources) == 1
    assert sources[0].headline == "A Useful Post"
    assert sources[0].content == "This post makes several claims about X."
    assert (sources[0].extra or {}).get("url") == "https://example.com/post"

    run_spy.assert_called_once()


async def test_run_ingest_without_target_just_saves_source(tmp_db, mocker):
    """When no target_question_id is supplied, the bg task still saves the
    source page but skips the ingest extraction call."""
    scraped = ScrapedPage(
        url="https://example.com/post",
        title="Orphan Source",
        content="Just a source, no extraction.",
        fetched_at="2026-01-01T00:00:00+00:00",
    )
    mocker.patch("rumil.api.chat.scrape_url", return_value=scraped)
    run_spy = mocker.patch("rumil.calls.ingest.IngestCall.run", return_value=None)

    _, content = await _run_ingest_and_wait(
        tmp_db,
        None,
        {"url": "https://example.com/post", "target_question_id": None},
    )
    assert content is not None
    assert content["status"] == "completed"
    assert "Created source page" in content["summary"]

    sources = await tmp_db.get_pages(page_type=PageType.SOURCE)
    assert len(sources) == 1
    assert run_spy.call_count == 0


async def test_run_ingest_scrape_failure_writes_failed_completion(tmp_db, seeded_question, mocker):
    """A failed scrape (scrape_url returns None) produces a failed
    DISPATCH_RESULT row — the bg task never raises."""
    mocker.patch("rumil.api.chat.scrape_url", return_value=None)

    _, content = await _run_ingest_and_wait(
        tmp_db,
        seeded_question.id,
        {
            "url": "https://example.com/broken",
            "target_question_id": seeded_question.id[:8],
        },
    )
    assert content is not None
    assert content["status"] == "failed"
    assert "Failed to fetch URL" in content["error"]


async def test_run_ingest_catches_scrape_exception(tmp_db, seeded_question, mocker):
    """If scrape_url raises (network blowup, parser crash), the bg task
    writes a failed DISPATCH_RESULT row."""
    mocker.patch("rumil.api.chat.scrape_url", side_effect=RuntimeError("boom"))

    _, content = await _run_ingest_and_wait(
        tmp_db,
        seeded_question.id,
        {
            "url": "https://example.com/explode",
            "target_question_id": seeded_question.id[:8],
        },
    )
    assert content is not None
    assert content["status"] == "failed"
    assert "boom" in content["error"]


async def test_run_ingest_catches_runner_failure(tmp_db, seeded_question, mocker):
    """If the IngestCall runner raises, the source page is still saved and
    the DISPATCH_RESULT row records completion with a failure note in the
    summary (the outer run reached the completion path — only the inner
    extraction call raised)."""
    scraped = ScrapedPage(
        url="https://example.com/post",
        title="Fine Scrape",
        content="Scrape worked, extraction won't.",
        fetched_at="2026-01-01T00:00:00+00:00",
    )
    mocker.patch("rumil.api.chat.scrape_url", return_value=scraped)
    mocker.patch(
        "rumil.calls.ingest.IngestCall.run",
        side_effect=RuntimeError("runner crash"),
    )

    _, content = await _run_ingest_and_wait(
        tmp_db,
        seeded_question.id,
        {
            "url": "https://example.com/post",
            "target_question_id": seeded_question.id[:8],
        },
    )
    assert content is not None
    assert content["status"] == "completed"
    assert "runner crash" in content["summary"]
    sources = await tmp_db.get_pages(page_type=PageType.SOURCE)
    assert len(sources) == 1


async def test_run_ingest_unknown_target_returns_string(tmp_db, mocker):
    """If the target question short ID doesn't resolve, the bg task saves
    the source and records completion with a 'not found' note."""
    scraped = ScrapedPage(
        url="https://example.com/post",
        title="A Post",
        content="Body",
        fetched_at="2026-01-01T00:00:00+00:00",
    )
    mocker.patch("rumil.api.chat.scrape_url", return_value=scraped)
    run_spy = mocker.patch("rumil.calls.ingest.IngestCall.run", return_value=None)

    _, content = await _run_ingest_and_wait(
        tmp_db,
        None,
        {
            "url": "https://example.com/post",
            "target_question_id": "deadbeef",
        },
    )
    assert content is not None
    assert content["status"] == "completed"
    assert "not found" in content["summary"]
    assert run_spy.call_count == 0


async def test_execute_tool_timed_logs_on_cancelled_and_reraises(
    tmp_db, seeded_question, mocker, caplog
):
    """When the inner _execute_tool is cancelled (client disconnect), we
    must log a warning with the elapsed time and re-raise so the request
    terminates cleanly."""

    async def slow_then_cancel(*args, **kwargs):
        await asyncio.sleep(0)
        raise asyncio.CancelledError()

    mocker.patch("rumil.api.chat._execute_tool", side_effect=slow_then_cancel)

    with (
        caplog.at_level(logging.WARNING, logger="rumil.api.chat"),
        pytest.raises(asyncio.CancelledError),
    ):
        await _execute_tool_timed(
            "ingest_source",
            {"url": "https://example.com/slow"},
            tmp_db,
            seeded_question.id,
        )

    cancelled_records = [r for r in caplog.records if "cancelled mid-execution" in r.message]
    assert len(cancelled_records) == 1
    rec = cancelled_records[0]
    assert rec.levelname == "WARNING"
    assert "ingest_source" in rec.message
    assert getattr(rec, "tool_name", None) == "ingest_source"
    assert getattr(rec, "elapsed_ms", None) is not None
    assert getattr(rec, "tool_input", None) == {"url": "https://example.com/slow"}


async def test_execute_tool_timed_passthrough_on_success(tmp_db, seeded_question, mocker):
    """Sanity: on a normal call, _execute_tool_timed returns whatever
    _execute_tool returned, unchanged."""
    mocker.patch(
        "rumil.api.chat._execute_tool",
        return_value="ok",
    )
    result = await _execute_tool_timed(
        "list_workspace",
        {},
        tmp_db,
        seeded_question.id,
    )
    assert result == "ok"
