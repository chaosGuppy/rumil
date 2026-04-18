"""Tests for the orchestrator triage gate.

When a question has been auto-triaged as a duplicate (or low-fertility),
running an orchestrator against it should abort without dispatching any
calls. A setting provides an override for operators who want to
investigate anyway.
"""

from __future__ import annotations

import pytest

from rumil.database import DB
from rumil.models import Page, PageLayer, PageType, Workspace
from rumil.orchestrators.common import check_triage_before_run
from rumil.orchestrators.two_phase import TwoPhaseOrchestrator
from rumil.settings import override_settings


async def _save_question(
    db: DB,
    *,
    triage: dict | None = None,
) -> Page:
    extra: dict = {}
    if triage is not None:
        extra["triage"] = triage
    page = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="body",
        headline="test question",
        extra=extra,
    )
    await db.save_page(page)
    return page


async def test_gate_proceeds_when_no_triage(tmp_db):
    question = await _save_question(tmp_db, triage=None)
    assert await check_triage_before_run(tmp_db, question.id) is True


async def test_gate_aborts_on_duplicate(tmp_db):
    question = await _save_question(
        tmp_db,
        triage={
            "is_duplicate": True,
            "duplicate_of": "11111111-2222-3333-4444-555555555555",
            "fertility_score": 5,
        },
    )
    assert await check_triage_before_run(tmp_db, question.id) is False


async def test_gate_ignores_duplicate_without_target(tmp_db):
    question = await _save_question(
        tmp_db,
        triage={
            "is_duplicate": True,
            "duplicate_of": None,
            "fertility_score": 5,
        },
    )
    assert await check_triage_before_run(tmp_db, question.id) is True


async def test_gate_proceeds_when_override_set(tmp_db):
    question = await _save_question(
        tmp_db,
        triage={
            "is_duplicate": True,
            "duplicate_of": "orig-uuid",
            "fertility_score": 1,
        },
    )
    with override_settings(orchestrator_ignore_triage=True):
        assert await check_triage_before_run(tmp_db, question.id) is True


async def test_gate_aborts_on_low_fertility_default(tmp_db):
    question = await _save_question(
        tmp_db,
        triage={
            "is_duplicate": False,
            "duplicate_of": None,
            "fertility_score": 1,
        },
    )
    assert await check_triage_before_run(tmp_db, question.id) is False


async def test_gate_proceeds_when_fertility_threshold_lowered(tmp_db):
    question = await _save_question(
        tmp_db,
        triage={
            "is_duplicate": False,
            "duplicate_of": None,
            "fertility_score": 1,
        },
    )
    with override_settings(orchestrator_respect_triage_min_fertility=1):
        assert await check_triage_before_run(tmp_db, question.id) is True


async def test_gate_proceeds_on_healthy_triage(tmp_db):
    question = await _save_question(
        tmp_db,
        triage={
            "is_duplicate": False,
            "duplicate_of": None,
            "fertility_score": 4,
        },
    )
    assert await check_triage_before_run(tmp_db, question.id) is True


async def test_gate_proceeds_when_question_missing(tmp_db):
    assert (
        await check_triage_before_run(
            tmp_db,
            "00000000-0000-0000-0000-000000000000",
        )
        is True
    )


async def test_two_phase_orchestrator_aborts_on_duplicate_triage(tmp_db, mocker):
    """Running TwoPhaseOrchestrator on a triage-duplicate question aborts
    before any calls are dispatched. No forks, no LLM work, nothing."""
    question = await _save_question(
        tmp_db,
        triage={
            "is_duplicate": True,
            "duplicate_of": "11111111-2222-3333-4444-555555555555",
            "fertility_score": 5,
        },
    )

    fork_mock = mocker.patch.object(tmp_db, "fork")
    get_next_batch = mocker.patch.object(
        TwoPhaseOrchestrator,
        "_get_next_batch",
    )

    orch = TwoPhaseOrchestrator(tmp_db)
    await orch.run(question.id)

    fork_mock.assert_not_called()
    get_next_batch.assert_not_called()


async def test_two_phase_orchestrator_aborts_on_low_fertility(tmp_db, mocker):
    question = await _save_question(
        tmp_db,
        triage={
            "is_duplicate": False,
            "duplicate_of": None,
            "fertility_score": 1,
        },
    )

    fork_mock = mocker.patch.object(tmp_db, "fork")
    get_next_batch = mocker.patch.object(
        TwoPhaseOrchestrator,
        "_get_next_batch",
    )

    orch = TwoPhaseOrchestrator(tmp_db)
    await orch.run(question.id)

    fork_mock.assert_not_called()
    get_next_batch.assert_not_called()


@pytest.mark.parametrize(
    ("triage_payload", "expected_proceed"),
    [
        (None, True),
        ({"is_duplicate": False, "fertility_score": 5}, True),
        (
            {
                "is_duplicate": True,
                "duplicate_of": "orig",
                "fertility_score": 5,
            },
            False,
        ),
        (
            {
                "is_duplicate": False,
                "fertility_score": 1,
            },
            False,
        ),
    ],
)
async def test_gate_matrix(tmp_db, triage_payload, expected_proceed):
    question = await _save_question(tmp_db, triage=triage_payload)
    assert await check_triage_before_run(tmp_db, question.id) is expected_proceed
