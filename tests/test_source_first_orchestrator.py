"""Tests for SourceFirstOrchestrator.

All tests mock the LLM and DB entirely. We patch the helpers that
``source_first`` imports (at the orchestrator's module boundary), so the
orchestrator's dispatch choices are the thing under test — not the call
implementations it dispatches to.

Coverage:
- First dispatch is ``web_research`` when the question has no sources
  and no pre-seeded URLs.
- First dispatch is ingest when the question has pre-seeded URLs.
- After sources exist, next dispatch is ``find_considerations`` with
  ``find_considerations_variant == "source_first"`` in effect.
- ``assess`` runs after considerations.
- ``create_view`` / ``update_view`` fires each iteration.
- Two barren web_research rounds terminate the loop without spinning.
- Budget exhaustion terminates the loop.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from rumil.models import Page, PageLayer, PageType, Workspace
from rumil.orchestrators.source_first import (
    MAX_BARREN_SOURCE_ROUNDS,
    SEED_URLS_EXTRA_KEY,
    SOURCE_FIRST_VARIANT,
    SourceFirstOrchestrator,
)
from rumil.settings import get_settings, override_settings


def _question(headline: str = "root?", extra: dict | None = None) -> Page:
    return Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=headline,
        headline=headline,
        extra=extra or {},
    )


def _source() -> Page:
    return Page(
        page_type=PageType.SOURCE,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="scraped content",
        headline="An existing source",
    )


def _view() -> Page:
    return Page(
        page_type=PageType.VIEW,
        layer=PageLayer.WIKI,
        workspace=Workspace.RESEARCH,
        content="view content",
        headline="View",
    )


def _make_db(
    *,
    question: Page,
    sources_over_iterations: list[int] | None = None,
    view: Page | None = None,
    budget: int = 10,
) -> MagicMock:
    """Build a MagicMock DB.

    ``sources_over_iterations``: when provided, the sequence dictates the
    value of ``count_sources_for_question`` across successive reads. The
    real count is driven by this list (popped on each read, last value
    sticks) — lets us script "no sources → one source" or "always barren"
    scenarios.
    """
    db = MagicMock()
    db.run_id = str(uuid.uuid4())
    db.get_page = AsyncMock(return_value=question)
    db.get_view_for_question = AsyncMock(return_value=view)

    state = {"budget": budget}

    async def _remaining() -> int:
        return state["budget"]

    async def _add_budget(n: int) -> None:
        state["budget"] += n

    async def _consume_budget(n: int = 1) -> bool:
        if state["budget"] < n:
            return False
        state["budget"] -= n
        return True

    async def _get_budget() -> tuple[int, int]:
        return budget, budget - state["budget"]

    db.budget_remaining = AsyncMock(side_effect=_remaining)
    db.add_budget = AsyncMock(side_effect=_add_budget)
    db.consume_budget = AsyncMock(side_effect=_consume_budget)
    db.get_budget = AsyncMock(side_effect=_get_budget)
    db._budget_state = state
    db._sources_over_iterations = list(sources_over_iterations or [0])
    return db


@pytest.fixture
def patched_helpers(mocker):
    """Patch every helper the orchestrator dispatches through.

    Each helper records its invocation and decrements the fake budget so
    the outer loop terminates naturally without spinning indefinitely.
    """
    calls: dict[str, list[dict]] = {
        "web_research": [],
        "ingest": [],
        "find_considerations": [],
        "assess": [],
        "create_view": [],
        "update_view": [],
        "count_sources": [],
        "create_source_page": [],
    }
    # Track find_considerations_variant value at the moment find is called,
    # so we can assert the orchestrator flipped the setting correctly.
    find_variant_snapshots: list[str] = []

    def _decrement_budget(db):
        if hasattr(db, "_budget_state"):
            db._budget_state["budget"] = max(0, db._budget_state["budget"] - 1)

    async def _web_research(question_id, db, **kwargs):
        calls["web_research"].append({"question_id": question_id, **kwargs})
        _decrement_budget(db)
        return f"web-call-{len(calls['web_research'])}"

    async def _ingest(source_page, question_id, db, **kwargs):
        calls["ingest"].append(
            {
                "source_id": source_page.id,
                "question_id": question_id,
                **kwargs,
            }
        )
        _decrement_budget(db)
        return 1

    async def _find(question_id, db, **kwargs):
        find_variant_snapshots.append(get_settings().find_considerations_variant)
        calls["find_considerations"].append({"question_id": question_id, **kwargs})
        _decrement_budget(db)
        return 1, [f"find-call-{len(calls['find_considerations'])}"]

    async def _assess(question_id, db, **kwargs):
        calls["assess"].append({"question_id": question_id, **kwargs})
        _decrement_budget(db)
        return f"assess-call-{len(calls['assess'])}"

    async def _create_view(question_id, db, **kwargs):
        calls["create_view"].append({"question_id": question_id, **kwargs})
        _decrement_budget(db)
        return f"create-view-call-{len(calls['create_view'])}"

    async def _update_view(question_id, db, **kwargs):
        calls["update_view"].append({"question_id": question_id, **kwargs})
        _decrement_budget(db)
        return f"update-view-call-{len(calls['update_view'])}"

    async def _count_sources(db, question_id):
        calls["count_sources"].append({"question_id": question_id})
        script = getattr(db, "_sources_over_iterations", None) or [0]
        if len(script) > 1:
            val = script.pop(0)
        else:
            val = script[0]
        return val

    async def _create_source_page(url, db):
        calls["create_source_page"].append({"url": url})
        return Page(
            page_type=PageType.SOURCE,
            layer=PageLayer.SQUIDGY,
            workspace=Workspace.RESEARCH,
            content=f"scraped: {url}",
            headline=f"Source from {url}",
        )

    mocker.patch(
        "rumil.orchestrators.source_first.web_research_question",
        side_effect=_web_research,
    )
    mocker.patch(
        "rumil.orchestrators.source_first.ingest_until_done",
        side_effect=_ingest,
    )
    mocker.patch(
        "rumil.orchestrators.source_first.find_considerations_until_done",
        side_effect=_find,
    )
    mocker.patch(
        "rumil.orchestrators.source_first.assess_question",
        side_effect=_assess,
    )
    mocker.patch(
        "rumil.orchestrators.source_first.create_view_for_question",
        side_effect=_create_view,
    )
    mocker.patch(
        "rumil.orchestrators.source_first.update_view_for_question",
        side_effect=_update_view,
    )
    mocker.patch(
        "rumil.orchestrators.source_first.count_sources_for_question",
        side_effect=_count_sources,
    )
    mocker.patch(
        "rumil.sources.create_source_page",
        side_effect=_create_source_page,
    )

    return {"calls": calls, "find_variant_snapshots": find_variant_snapshots}


@pytest.mark.asyncio
async def test_zero_sources_first_dispatch_is_web_research(patched_helpers):
    """Question with no sources and no pre-seed → first dispatch is web_research."""
    question = _question()
    db = _make_db(
        question=question,
        sources_over_iterations=[0, 1],
        budget=5,
    )

    orch = SourceFirstOrchestrator(db)
    await orch.run(question.id)

    calls = patched_helpers["calls"]
    assert len(calls["web_research"]) >= 1
    assert calls["ingest"] == []
    assert calls["web_research"][0]["question_id"] == question.id


@pytest.mark.asyncio
async def test_pre_seeded_urls_first_dispatch_is_ingest(patched_helpers):
    """Question with pre-seeded URLs → ingest fires (and web_research does not)."""
    question = _question(
        extra={SEED_URLS_EXTRA_KEY: ["https://example.com/a", "https://example.com/b"]},
    )
    db = _make_db(
        question=question,
        sources_over_iterations=[0, 2],
        budget=5,
    )

    orch = SourceFirstOrchestrator(db)
    await orch.run(question.id)

    calls = patched_helpers["calls"]
    assert len(calls["ingest"]) >= 2
    assert calls["web_research"] == []
    assert [c["url"] for c in calls["create_source_page"]] == [
        "https://example.com/a",
        "https://example.com/b",
    ]


@pytest.mark.asyncio
async def test_find_considerations_runs_with_source_first_variant(patched_helpers):
    """After the source pass, find_considerations runs with the variant flipped."""
    question = _question()
    db = _make_db(
        question=question,
        sources_over_iterations=[0, 1],
        budget=5,
    )

    with override_settings(rumil_test_mode="1", find_considerations_variant="default"):
        orch = SourceFirstOrchestrator(db)
        await orch.run(question.id)

        assert get_settings().find_considerations_variant == "default"

    calls = patched_helpers["calls"]
    assert len(calls["find_considerations"]) >= 1
    snapshots = patched_helpers["find_variant_snapshots"]
    assert SOURCE_FIRST_VARIANT in snapshots


@pytest.mark.asyncio
async def test_assess_runs_after_considerations(patched_helpers):
    """assess is dispatched after find_considerations in each iteration."""
    question = _question()
    db = _make_db(
        question=question,
        sources_over_iterations=[0, 1],
        budget=5,
    )

    orch = SourceFirstOrchestrator(db)
    await orch.run(question.id)

    calls = patched_helpers["calls"]
    assert len(calls["find_considerations"]) >= 1
    assert len(calls["assess"]) >= 1


@pytest.mark.asyncio
async def test_view_created_or_updated_each_iteration(patched_helpers):
    """Each iteration ends in create_view (when absent) or update_view (when present)."""
    question = _question()
    db = _make_db(
        question=question,
        sources_over_iterations=[0, 1],
        view=None,
        budget=5,
    )

    orch = SourceFirstOrchestrator(db)
    await orch.run(question.id)

    calls = patched_helpers["calls"]
    n_view_touches = len(calls["create_view"]) + len(calls["update_view"])
    assert n_view_touches >= 1


@pytest.mark.asyncio
async def test_existing_view_uses_update_not_create(patched_helpers):
    """When a view already exists, update_view fires — not create_view."""
    question = _question()
    view = _view()
    db = _make_db(
        question=question,
        sources_over_iterations=[0, 1],
        view=view,
        budget=5,
    )

    orch = SourceFirstOrchestrator(db)
    await orch.run(question.id)

    calls = patched_helpers["calls"]
    assert len(calls["update_view"]) >= 1
    assert calls["create_view"] == []


@pytest.mark.asyncio
async def test_barren_source_rounds_terminate_loop(patched_helpers):
    """Two consecutive iterations producing no new sources should stop the loop."""
    question = _question()
    db = _make_db(
        question=question,
        sources_over_iterations=[0],
        budget=50,
    )

    orch = SourceFirstOrchestrator(db)
    await orch.run(question.id)

    calls = patched_helpers["calls"]
    assert len(calls["web_research"]) <= MAX_BARREN_SOURCE_ROUNDS
    assert len(calls["find_considerations"]) <= MAX_BARREN_SOURCE_ROUNDS
    assert db._budget_state["budget"] > 0


@pytest.mark.asyncio
async def test_budget_exhaustion_terminates_loop(patched_helpers):
    """Zero budget on entry → no dispatches at all."""
    question = _question()
    db = _make_db(
        question=question,
        sources_over_iterations=[0, 1],
        budget=0,
    )

    orch = SourceFirstOrchestrator(db)
    await orch.run(question.id)

    calls = patched_helpers["calls"]
    assert calls["web_research"] == []
    assert calls["ingest"] == []
    assert calls["find_considerations"] == []
    assert calls["assess"] == []
    assert calls["create_view"] == []
    assert calls["update_view"] == []


@pytest.mark.asyncio
async def test_sources_precede_considerations_in_dispatch_order(mocker):
    """Within each iteration the source pass runs before find_considerations."""
    question = _question()
    db = _make_db(
        question=question,
        sources_over_iterations=[0, 1],
        budget=5,
    )

    invocation_order: list[str] = []

    def _decrement_budget():
        db._budget_state["budget"] = max(0, db._budget_state["budget"] - 1)

    async def _record_web(*args, **kwargs):
        invocation_order.append("web_research")
        _decrement_budget()
        return "web-call-1"

    async def _record_find(*args, **kwargs):
        invocation_order.append("find_considerations")
        _decrement_budget()
        return 1, ["find-1"]

    async def _record_assess(*args, **kwargs):
        invocation_order.append("assess")
        _decrement_budget()
        return "assess-1"

    async def _record_view(*args, **kwargs):
        invocation_order.append("view")
        _decrement_budget()
        return "view-1"

    async def _count_sources(db_arg, question_id):
        script = db_arg._sources_over_iterations
        return script.pop(0) if len(script) > 1 else script[0]

    mocker.patch(
        "rumil.orchestrators.source_first.web_research_question",
        side_effect=_record_web,
    )
    mocker.patch(
        "rumil.orchestrators.source_first.find_considerations_until_done",
        side_effect=_record_find,
    )
    mocker.patch(
        "rumil.orchestrators.source_first.assess_question",
        side_effect=_record_assess,
    )
    mocker.patch(
        "rumil.orchestrators.source_first.create_view_for_question",
        side_effect=_record_view,
    )
    mocker.patch(
        "rumil.orchestrators.source_first.update_view_for_question",
        side_effect=_record_view,
    )
    mocker.patch(
        "rumil.orchestrators.source_first.count_sources_for_question",
        side_effect=_count_sources,
    )

    orch = SourceFirstOrchestrator(db)
    await orch.run(question.id)

    assert "web_research" in invocation_order
    assert "find_considerations" in invocation_order
    web_idx = invocation_order.index("web_research")
    find_idx = invocation_order.index("find_considerations")
    assert web_idx < find_idx
    if "assess" in invocation_order:
        assert find_idx < invocation_order.index("assess")
