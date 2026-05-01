"""Tests for the Workflow protocol and TwoPhaseWorkflow adapter.

The point of these tests is to pin the protocol shape and the contract
``TwoPhaseWorkflow`` is supposed to satisfy. Real orchestrator runs are
exercised via the LLM-backed integration tests / the manual versus
script paths; here we only check that the wrapper's plumbing is right.
"""

from __future__ import annotations

import pytest

from rumil.versus_workflow import TwoPhaseWorkflow, Workflow


def test_two_phase_workflow_class_attrs():
    wf = TwoPhaseWorkflow(budget=10)
    assert wf.name == "two_phase"
    assert wf.produces_artifact is False
    assert isinstance(wf.code_paths, tuple)
    assert len(wf.code_paths) > 0


def test_two_phase_workflow_satisfies_runtime_protocol():
    assert isinstance(TwoPhaseWorkflow(budget=4), Workflow)


def test_fingerprint_includes_kind_and_budget():
    wf = TwoPhaseWorkflow(budget=7)
    fp = wf.fingerprint()
    assert fp["kind"] == "two_phase"
    assert fp["budget"] == 7


def test_fingerprint_changes_with_budget():
    a = TwoPhaseWorkflow(budget=4).fingerprint()
    b = TwoPhaseWorkflow(budget=8).fingerprint()
    assert a != b


@pytest.mark.asyncio
async def test_setup_seeds_budget(mocker):
    db = mocker.MagicMock()
    db.init_budget = mocker.AsyncMock()

    wf = TwoPhaseWorkflow(budget=10)
    await wf.setup(db, "q-1")

    db.init_budget.assert_called_once_with(10)


@pytest.mark.asyncio
async def test_run_constructs_orch_with_budget_cap_and_runs(mocker):
    """The wrapper must thread budget into ``budget_cap`` and call run(qid)."""
    fake_orch = mocker.MagicMock()
    fake_orch.run = mocker.AsyncMock()
    orch_cls = mocker.MagicMock(return_value=fake_orch)
    mocker.patch.object(TwoPhaseWorkflow, "orch_cls", orch_cls)

    db = mocker.MagicMock()
    broadcaster = mocker.MagicMock()

    wf = TwoPhaseWorkflow(budget=5)
    await wf.run(db, "q-1", broadcaster)

    orch_cls.assert_called_once_with(db=db, broadcaster=broadcaster, budget_cap=5)
    fake_orch.run.assert_called_once_with("q-1")
