"""Tests for the Workflow protocol and TwoPhaseWorkflow adapter.

The point of these tests is to pin the protocol shape and the contract
``TwoPhaseWorkflow`` is supposed to satisfy. Real orchestrator runs are
exercised via the LLM-backed integration tests / the manual versus
script paths; here we only check that the wrapper's plumbing is right.
"""

from collections.abc import Mapping

import pytest

from rumil.settings import override_settings
from rumil.versus_workflow import TwoPhaseWorkflow, Workflow, _BudgetedOrchWorkflow


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


def test_fingerprint_includes_settings_snapshot():
    """Post-#424: workflow fingerprint folds in a snapshot of behaviour-
    affecting settings so an orchestrator-level setting flip auto-forks
    the dedup key without manual version-bumps. Pin the contract: every
    name in ``relevant_settings`` is present as ``settings.<name>`` in
    the fingerprint dict.
    """
    wf = TwoPhaseWorkflow(budget=4)
    fp = wf.fingerprint()
    for name in wf.relevant_settings:
        assert f"settings.{name}" in fp


def test_fingerprint_forks_when_relevant_setting_changes():
    """Flipping a setting listed in ``relevant_settings`` must change
    the fingerprint. ``enable_red_team`` defaults False; flip to True
    via override_settings and confirm divergence.
    """
    wf = TwoPhaseWorkflow(budget=4)
    baseline = dict(wf.fingerprint())
    with override_settings(enable_red_team=True):
        forked = dict(wf.fingerprint())
    assert baseline != forked
    assert baseline["settings.enable_red_team"] is False
    assert forked["settings.enable_red_team"] is True


def test_fingerprint_unaffected_by_unlisted_setting():
    """Settings not in ``relevant_settings`` must NOT show up in the
    fingerprint — otherwise unrelated changes (e.g. an unrelated
    timeout knob) would flap the dedup hash.
    """
    wf = TwoPhaseWorkflow(budget=4)
    fp = wf.fingerprint()
    assert "settings.frontend_url" not in fp
    assert "settings.max_db_retries" not in fp


# Workflow attrs that are protocol metadata or class-level config —
# they're hashed via ``code_paths`` / surface as ``kind`` in the
# fingerprint, not as instance fields. Update this set when the
# protocol gains new opaque fields (e.g. ``produces_artifact`` is
# protocol-level metadata that doesn't need its own fingerprint key
# because it doesn't change behaviour for a fixed workflow class).
_WORKFLOW_ATTRS_OPAQUE_TO_FINGERPRINT = {
    "name",  # surfaced via ``kind``
    "code_paths",  # consumed by compute_workflow_code_fingerprint
    "produces_artifact",  # protocol metadata, not a per-instance knob
    "orch_cls",  # class reference, covered via code_paths
    "relevant_settings",  # the names; their *values* fold in via fingerprint()
    "last_status",  # set by run(); not an init-time knob
}


def _public_attrs_for_workflow(workflow: object) -> set[str]:
    """Public instance + class attrs on ``workflow`` excluding callables,
    dunders, and protocol-metadata fields. Used by the introspection
    test below.
    """
    out: set[str] = set()
    for attr in dir(workflow):
        if attr.startswith("_"):
            continue
        value = getattr(workflow, attr)
        if callable(value):
            continue
        out.add(attr)
    return out


def _fingerprint_keys_normalized(fp: Mapping[str, object]) -> set[str]:
    """Strip ``settings.`` prefix and add the ``name``→``kind`` alias
    so fingerprint keys can be compared against attribute names.
    """
    keys = {k.removeprefix("settings.") for k in fp}
    if "kind" in keys:
        keys.add("name")
    return keys


def test_workflow_fingerprint_covers_all_public_fields():
    """Catches drift: every public instance/class field on
    ``TwoPhaseWorkflow`` must appear in ``fingerprint()`` output (or
    be explicitly opaque). Adding ``self.foo = ...`` to ``__init__``
    without folding it into ``fingerprint()`` fails this test.
    """
    wf = TwoPhaseWorkflow(budget=4)
    fp = wf.fingerprint()
    public_attrs = _public_attrs_for_workflow(wf) - _WORKFLOW_ATTRS_OPAQUE_TO_FINGERPRINT
    fp_keys = _fingerprint_keys_normalized(fp)
    missing = public_attrs - fp_keys
    assert not missing, f"Workflow fields missing from fingerprint(): {missing}"


def test_introspection_helper_flags_omitted_field():
    """Pin the introspection helper itself: a synthetic workflow with
    a deliberately-not-fingerprinted instance attr must be flagged.
    """

    class _BadWorkflow(_BudgetedOrchWorkflow):
        name = "bad"
        code_paths = ()
        orch_cls = object  # placeholder; never instantiated
        relevant_settings = ()

        def __init__(self, *, budget: int, secret_knob: str) -> None:
            super().__init__(budget=budget)
            self.secret_knob = secret_knob

        # Deliberately doesn't fold ``secret_knob`` into fingerprint().

    wf = _BadWorkflow(budget=4, secret_knob="oops")
    fp = wf.fingerprint()
    public_attrs = _public_attrs_for_workflow(wf) - _WORKFLOW_ATTRS_OPAQUE_TO_FINGERPRINT
    fp_keys = _fingerprint_keys_normalized(fp)
    missing = public_attrs - fp_keys
    assert "secret_knob" in missing


@pytest.mark.asyncio
async def test_setup_seeds_budget(mocker):
    db = mocker.MagicMock()
    db.init_budget = mocker.AsyncMock()

    wf = TwoPhaseWorkflow(budget=10)
    await wf.setup(db, "q-1")

    db.init_budget.assert_called_once_with(10)


@pytest.mark.asyncio
async def test_run_constructs_orch_with_assigned_budget_and_runs(mocker):
    """The wrapper must thread budget into ``assigned_budget`` and call run(qid)."""
    fake_orch = mocker.MagicMock()
    fake_orch.run = mocker.AsyncMock()
    orch_cls = mocker.MagicMock(return_value=fake_orch)
    mocker.patch.object(TwoPhaseWorkflow, "orch_cls", orch_cls)

    db = mocker.MagicMock()
    broadcaster = mocker.MagicMock()

    wf = TwoPhaseWorkflow(budget=5)
    await wf.run(db, "q-1", broadcaster)

    orch_cls.assert_called_once_with(db=db, broadcaster=broadcaster, assigned_budget=5)
    fake_orch.run.assert_called_once_with("q-1")
