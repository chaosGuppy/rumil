"""Tests for DistillFirstOrchestrator.

LLM and DB are mocked entirely. We mock at the top of the orchestrators.common
helper module (not at the DB or LLM layer) so tests exercise the orchestrator's
dispatch decisions without touching the database or hitting the network.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from rumil.models import (
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)
from rumil.orchestrators.distill_first import (
    SPARSE_PAGE_THRESHOLD,
    DistillFirstOrchestrator,
)


def _question(headline: str = "root?") -> Page:
    return Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=headline,
        headline=headline,
    )


def _view(question_id: str) -> Page:
    return Page(
        page_type=PageType.VIEW,
        layer=PageLayer.WIKI,
        workspace=Workspace.RESEARCH,
        content="view content",
        headline=f"View of {question_id[:8]}",
        sections=["key_findings"],
    )


def _claim(credence: int | None = None) -> Page:
    return Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="some claim",
        headline="Some claim",
        credence=credence,
        robustness=3 if credence is not None else None,
    )


def _view_item_link(view_id: str, item_id: str, importance: int | None = 3) -> PageLink:
    return PageLink(
        from_page_id=view_id,
        to_page_id=item_id,
        link_type=LinkType.VIEW_ITEM,
        importance=importance,
        section="key_findings",
        position=0,
    )


def _make_db(
    *,
    view: Page | None = None,
    considerations: list[tuple[Page, PageLink]] | None = None,
    children: list[Page] | None = None,
    view_items: list[tuple[Page, PageLink]] | None = None,
    judgements_by_q: dict[str, list[Page]] | None = None,
    budget: int = 10,
) -> MagicMock:
    db = MagicMock()
    db.run_id = str(uuid.uuid4())
    db.get_view_for_question = AsyncMock(return_value=view)
    db.get_considerations_for_question = AsyncMock(return_value=considerations or [])
    db.get_child_questions = AsyncMock(return_value=children or [])
    db.get_view_items = AsyncMock(return_value=view_items or [])
    db.get_judgements_for_questions = AsyncMock(return_value=judgements_by_q or {})

    state = {"budget": budget}

    async def _remaining() -> int:
        return state["budget"]

    async def _consume(n: int = 1) -> None:
        state["budget"] = max(0, state["budget"] - n)

    async def _get_budget() -> tuple[int, int]:
        return 10, 10 - state["budget"]

    db.budget_remaining = AsyncMock(side_effect=_remaining)
    db.consume_budget = AsyncMock(side_effect=_consume)
    db.get_budget = AsyncMock(side_effect=_get_budget)
    db._budget_state = state
    return db


@pytest.fixture
def patched_helpers(mocker):
    """Patch the common-helper functions the orchestrator dispatches through.

    Each returns a fake call id. Each also decrements the fake budget so the
    outer loop terminates naturally.
    """

    def _make(name: str):
        call_ids: list[str] = []

        async def _impl(*args, **kwargs):
            cid = f"{name}-call-{len(call_ids)}"
            call_ids.append(cid)
            db = args[1] if len(args) > 1 else kwargs.get("db")
            if db is not None and hasattr(db, "_budget_state"):
                db._budget_state["budget"] = max(0, db._budget_state["budget"] - 1)
            return cid

        return _impl, call_ids

    create_impl, create_ids = _make("create_view")
    update_impl, update_ids = _make("update_view")
    assess_impl, assess_ids = _make("assess")

    async def _find_impl(question_id, db, *args, **kwargs):
        if hasattr(db, "_budget_state"):
            db._budget_state["budget"] = max(0, db._budget_state["budget"] - 1)
        return 1, [f"find-call-{question_id[:4]}"]

    mocker.patch(
        "rumil.orchestrators.distill_first.create_view_for_question",
        side_effect=create_impl,
    )
    mocker.patch(
        "rumil.orchestrators.distill_first.update_view_for_question",
        side_effect=update_impl,
    )
    mocker.patch(
        "rumil.orchestrators.distill_first.assess_question",
        side_effect=assess_impl,
    )
    find_mock = mocker.patch(
        "rumil.orchestrators.distill_first.find_considerations_until_done",
        side_effect=_find_impl,
    )

    return {
        "create_view_ids": create_ids,
        "update_view_ids": update_ids,
        "assess_ids": assess_ids,
        "find_mock": find_mock,
    }


@pytest.mark.asyncio
async def test_sparse_question_first_dispatch_is_create_view(patched_helpers):
    question = _question()
    db = _make_db(view=None, budget=1)

    orch = DistillFirstOrchestrator(db)
    await orch.run(question.id)

    assert len(patched_helpers["create_view_ids"]) == 1
    assert patched_helpers["assess_ids"] == []
    assert patched_helpers["find_mock"].call_count == 0


@pytest.mark.asyncio
async def test_existing_sparse_view_dispatches_find_considerations(patched_helpers):
    question = _question()
    view = _view(question.id)
    db = _make_db(
        view=view,
        considerations=[],
        children=[],
        view_items=[],
        budget=2,
    )

    orch = DistillFirstOrchestrator(db)
    await orch.run(question.id)

    assert patched_helpers["find_mock"].call_count >= 1
    assert patched_helpers["create_view_ids"] == []
    assert len(patched_helpers["update_view_ids"]) >= 1


@pytest.mark.asyncio
async def test_pages_without_credence_dispatch_assess(patched_helpers):
    question = _question()
    view = _view(question.id)
    unscored_claims = [_claim(credence=None) for _ in range(SPARSE_PAGE_THRESHOLD + 1)]
    considerations = [
        (
            c,
            PageLink(
                from_page_id=c.id,
                to_page_id=question.id,
                link_type=LinkType.CONSIDERATION,
            ),
        )
        for c in unscored_claims
    ]
    db = _make_db(
        view=view,
        considerations=considerations,
        children=[],
        view_items=[],
        budget=1,
    )

    orch = DistillFirstOrchestrator(db)
    await orch.run(question.id)

    assert len(patched_helpers["assess_ids"]) >= 1
    assert patched_helpers["find_mock"].call_count == 0


@pytest.mark.asyncio
async def test_update_view_called_after_mutation_dispatch(patched_helpers):
    question = _question()
    view = _view(question.id)
    scored_claims = [_claim(credence=5) for _ in range(SPARSE_PAGE_THRESHOLD + 1)]
    considerations = [
        (
            c,
            PageLink(
                from_page_id=c.id,
                to_page_id=question.id,
                link_type=LinkType.CONSIDERATION,
            ),
        )
        for c in scored_claims
    ]
    view_items = [(c, _view_item_link(view.id, c.id, importance=3)) for c in scored_claims]
    db = _make_db(
        view=view,
        considerations=considerations,
        children=[],
        view_items=view_items,
        budget=1,
    )

    orch = DistillFirstOrchestrator(db)
    await orch.run(question.id)

    assert len(patched_helpers["update_view_ids"]) >= 1


@pytest.mark.asyncio
async def test_budget_exhaustion_terminates_loop(patched_helpers):
    question = _question()
    db = _make_db(view=None, budget=0)

    orch = DistillFirstOrchestrator(db)
    await orch.run(question.id)

    assert patched_helpers["create_view_ids"] == []
    assert patched_helpers["assess_ids"] == []
    assert patched_helpers["find_mock"].call_count == 0


@pytest.mark.asyncio
async def test_child_question_without_judgement_dispatches_assess(patched_helpers):
    question = _question()
    view = _view(question.id)
    scored_claims = [_claim(credence=5) for _ in range(SPARSE_PAGE_THRESHOLD + 1)]
    considerations = [
        (
            c,
            PageLink(
                from_page_id=c.id,
                to_page_id=question.id,
                link_type=LinkType.CONSIDERATION,
            ),
        )
        for c in scored_claims
    ]
    child = _question("child?")
    db = _make_db(
        view=view,
        considerations=considerations,
        children=[child],
        view_items=[],
        judgements_by_q={child.id: []},
        budget=1,
    )

    orch = DistillFirstOrchestrator(db)
    await orch.run(question.id)

    assert len(patched_helpers["assess_ids"]) >= 1
