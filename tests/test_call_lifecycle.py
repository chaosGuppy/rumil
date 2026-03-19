"""Lifecycle tests for call types (assess, ingest, scout).

These are regression tests for the call refactor. They use real LLM calls
(Haiku in test mode) to verify end-to-end lifecycle wiring: context building,
page creation, closing review, and DB state transitions.
"""

import pytest
import pytest_asyncio

from rumil.calls.assess import AssessCall
from rumil.calls.common import complete_call, run_closing_review
from rumil.calls.ingest import IngestCall
from rumil.calls.scout import ScoutCall
from rumil.models import (
    Call,
    CallStatus,
    CallType,
    Page,
    PageLayer,
    PageType,
    ScoutMode,
    Workspace,
)


@pytest_asyncio.fixture
async def ingest_call(tmp_db, question_page):
    call = Call(
        call_type=CallType.INGEST,
        workspace=Workspace.RESEARCH,
        scope_page_id=question_page.id,
        status=CallStatus.PENDING,
    )
    await tmp_db.save_call(call)
    return call


@pytest_asyncio.fixture
async def source_page(tmp_db):
    page = Page(
        page_type=PageType.SOURCE,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="The sky appears blue due to Rayleigh scattering of sunlight.",
        headline="Rayleigh scattering explains blue sky",
        extra={"filename": "sky-science.txt", "char_count": 58},
    )
    await tmp_db.save_page(page)
    return page


@pytest.mark.llm
async def test_assess_lifecycle(tmp_db, question_page, assess_call):
    """Assess call completes with correct DB state and review data."""
    assess = AssessCall(question_page.id, assess_call, tmp_db)
    await assess.run()
    result, review = assess.result, assess.review

    refreshed = await tmp_db.get_call(assess_call.id)
    assert refreshed.status == CallStatus.COMPLETE
    assert refreshed.completed_at is not None
    assert refreshed.result_summary != ""

    assert assess_call.review_json is not None
    assert isinstance(assess_call.review_json, dict)

    trace = await tmp_db.get_call_trace(assess_call.id)
    event_types = [e.get("event") for e in trace]
    assert "context_built" in event_types

    context_event = next(e for e in trace if e.get("event") == "context_built")
    assert context_event.get("source_page_id") is None
    assert isinstance(context_event.get("working_context_page_ids"), list)


@pytest.mark.llm
async def test_ingest_lifecycle(tmp_db, question_page, ingest_call, source_page):
    """Ingest call processes source document and completes with correct DB state."""
    ingest = IngestCall(source_page, question_page.id, ingest_call, tmp_db)
    await ingest.run()
    result, review = ingest.result, ingest.review

    refreshed = await tmp_db.get_call(ingest_call.id)
    assert refreshed.status == CallStatus.COMPLETE
    assert refreshed.completed_at is not None
    assert "sky-science.txt" in refreshed.result_summary

    assert ingest_call.review_json is not None
    assert isinstance(ingest_call.review_json, dict)

    trace = await tmp_db.get_call_trace(ingest_call.id)
    event_types = [e.get("event") for e in trace]
    assert "context_built" in event_types

    context_event = next(e for e in trace if e.get("event") == "context_built")
    assert context_event.get("source_page_id") == source_page.id


@pytest.mark.integration
async def test_scout_lifecycle(tmp_db, question_page, scout_call):
    """Scout session runs rounds, checks fruit, and completes."""
    await tmp_db.init_budget(2)
    scout = ScoutCall(
        question_page.id,
        scout_call,
        tmp_db,
        max_rounds=2,
        fruit_threshold=4,
        mode=ScoutMode.ALTERNATE,
    )
    await scout.run()

    assert scout.rounds_completed >= 1

    refreshed = await tmp_db.get_call(scout_call.id)
    assert refreshed.status == CallStatus.COMPLETE
    assert refreshed.completed_at is not None
    assert "Scout session complete" in refreshed.result_summary
    assert refreshed.call_params is not None
    assert refreshed.call_params["mode"] == "alternate"

    trace = await tmp_db.get_call_trace(scout_call.id)
    event_types = [e.get("event") for e in trace]
    assert "context_built" in event_types
    assert "review_complete" in event_types


@pytest.mark.integration
async def test_scout_stops_on_budget_exhaustion(tmp_db, question_page, scout_call):
    """Scout session stops when budget runs out mid-loop."""
    await tmp_db.init_budget(1)
    scout = ScoutCall(
        question_page.id,
        scout_call,
        tmp_db,
        max_rounds=5,
        fruit_threshold=0,
    )
    await scout.run()

    assert scout.rounds_completed <= 1

    refreshed = await tmp_db.get_call(scout_call.id)
    assert refreshed.status == CallStatus.COMPLETE


async def test_complete_call(tmp_db, assess_call):
    """complete_call sets status, timestamp, and summary."""
    assert assess_call.status == CallStatus.PENDING
    assert assess_call.completed_at is None

    await complete_call(assess_call, tmp_db, "Test summary")

    assert assess_call.status == CallStatus.COMPLETE
    assert assess_call.completed_at is not None
    assert assess_call.result_summary == "Test summary"

    refreshed = await tmp_db.get_call(assess_call.id)
    assert refreshed.status == CallStatus.COMPLETE
    assert refreshed.result_summary == "Test summary"


@pytest.mark.integration
async def test_closing_review_saves_page_ratings(
    tmp_db, question_page, assess_call,
):
    """run_closing_review returns a review dict and saves page ratings."""
    review = await run_closing_review(
        assess_call,
        "Created a claim about sky color.",
        f"Question: {question_page.content}",
        loaded_page_ids=[question_page.id],
        db=tmp_db,
    )

    assert review is not None
    assert "remaining_fruit" in review
    assert isinstance(review["remaining_fruit"], int)
    assert "confidence_in_output" in review
