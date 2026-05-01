"""Tests for the ``run_completions.py`` orch dispatch path and
the ``rumil_completion`` helpers it calls.

Heavy mocking everywhere — orch completions cost real money per run,
so tests stop short of the actual workflow / closer firing. Focus is
on:

- The CLI dispatches to ``run_orch_completion`` when ``--orch`` is
  passed (and falls through to single-shot when absent).
- The source_id format matches the decided convention
  ``orch:<workflow>:<model>:c<hash8>``.
- The driver wires its inputs end-to-end (workspace lookup, planning,
  per-essay run firing) without touching the network.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_VERSUS_SRC = Path(__file__).resolve().parents[1] / "versus" / "src"
if str(_VERSUS_SRC) not in sys.path:
    sys.path.insert(0, str(_VERSUS_SRC))

from versus import rumil_completion  # noqa: E402


def test_build_source_id_matches_decided_convention():
    sid = rumil_completion.build_source_id(
        workflow_name="two_phase",
        model="claude-opus-4-7",
        config_hash="2937f03b7e8d4f12",
    )
    assert sid == "orch:two_phase:claude-opus-4-7:c2937f03b"


def test_build_source_id_truncates_hash_to_8_hex():
    """Pin the 8-hex truncation: longer config_hash inputs must collapse
    to the same suffix length so source_ids are stable in width."""
    sid_short = rumil_completion.build_source_id("w", "m", "abcdef12")
    sid_long = rumil_completion.build_source_id("w", "m", "abcdef1234567890")
    assert sid_short.endswith(":cabcdef12")
    assert sid_long.endswith(":cabcdef12")


def test_workflow_registry_includes_two_phase():
    assert "two_phase" in rumil_completion.WORKFLOW_REGISTRY
    cls, defaults = rumil_completion.WORKFLOW_REGISTRY["two_phase"]
    assert isinstance(defaults, dict)
    # The class itself is an importable workflow.
    assert hasattr(cls, "produces_artifact")


def test_make_workflow_and_task_constructs_valid_pair():
    workflow, task = rumil_completion._make_workflow_and_task("two_phase", budget=4)
    assert workflow.name == "two_phase"
    assert workflow.budget == 4
    assert task.name == "complete_essay"


def test_make_workflow_and_task_unknown_name_lists_registered():
    with pytest.raises(KeyError, match="registered"):
        rumil_completion._make_workflow_and_task("not_a_real_workflow", budget=4)


@pytest.mark.asyncio
async def test_run_orch_completion_unknown_workflow_exits(mocker):
    cfg = MagicMock()
    with pytest.raises(SystemExit, match="unknown workflow"):
        await rumil_completion.run_orch_completion(
            cfg,
            essays=[],
            workspace="ws",
            workflow_name="not_a_real_workflow",
            model="claude-opus-4-7",
            budget=4,
            prefix_cfg=MagicMock(),
        )


@pytest.mark.asyncio
async def test_run_orch_completion_dry_run_plans_without_firing(mocker):
    """End-to-end sketch: verify the dry-run path lists the planned
    completions and never fires a workflow run.
    """
    cfg = MagicMock()
    cfg.completion.length_tolerance = 0.1
    prefix_cfg = MagicMock()
    prefix_cfg.n_paragraphs = 3
    prefix_cfg.include_headers = True

    fake_essay = MagicMock()
    fake_essay.id = "essay-1"

    fake_prepared = MagicMock()
    fake_prepared.essay_id = "essay-1"
    fake_prepared.prefix_config_hash = "abc123def456ghi7"
    fake_prepared.prefix_markdown = "Essay opening text"
    fake_prepared.remainder_markdown = "x" * 2000
    fake_prepared.target_words = 400
    mocker.patch("versus.rumil_completion.prepare.prepare", return_value=fake_prepared)

    fake_project = MagicMock()
    fake_project.id = "project-uuid-12345678"
    fake_project.name = "test-ws"

    probe_db = MagicMock()
    probe_db.list_projects = AsyncMock(return_value=[fake_project])
    mocker.patch(
        "rumil.database.DB.create",
        new=AsyncMock(return_value=probe_db),
    )

    mocker.patch(
        "versus.versus_config.compute_workspace_state_hash",
        new=AsyncMock(return_value="ws-state-hash"),
    )
    mocker.patch(
        "versus.versus_config.compute_judge_code_fingerprint",
        return_value={"src/rumil/llm.py": "deadbeef"},
    )
    mocker.patch(
        "versus.model_config.get_model_config",
        return_value=MagicMock(to_record_dict=MagicMock(return_value={"temperature": 1.0})),
    )

    fake_versus_client = MagicMock()
    mocker.patch(
        "versus.rumil_completion.versus_db.get_client",
        return_value=fake_versus_client,
    )
    # No existing completions so all planned essays are pending.
    mocker.patch(
        "versus.rumil_completion.versus_db.iter_texts",
        return_value=iter([]),
    )

    fake_make_config = mocker.patch(
        "versus.versus_config.make_versus_config",
        return_value=({"workflow": "two_phase"}, "deadbeef0123abcd", "task/two_phase:m:cdeadbeef"),
    )

    run_versus_mock = mocker.patch(
        "rumil.versus_runner.run_versus",
        new=AsyncMock(),
    )

    await rumil_completion.run_orch_completion(
        cfg,
        essays=[fake_essay],
        workspace="test-ws",
        workflow_name="two_phase",
        model="claude-opus-4-7",
        budget=4,
        prefix_cfg=prefix_cfg,
        dry_run=True,
    )

    # Dry-run must not fire a workflow run.
    run_versus_mock.assert_not_awaited()
    # And must not have written any rows.
    fake_versus_client.table.assert_not_called()
    # But planning ran make_versus_config so the source_id was produced.
    fake_make_config.assert_called()


@pytest.mark.asyncio
async def test_run_orch_completion_skips_essays_with_existing_row(mocker):
    """An essay × prefix that already has a row at this source_id is
    skipped during planning. ``[info] no pending`` covers the empty
    case so planning short-circuits cleanly.
    """
    cfg = MagicMock()
    cfg.completion.length_tolerance = 0.1
    prefix_cfg = MagicMock()
    prefix_cfg.n_paragraphs = 3
    prefix_cfg.include_headers = True

    fake_essay = MagicMock()
    fake_essay.id = "essay-1"

    fake_prepared = MagicMock()
    fake_prepared.essay_id = "essay-1"
    fake_prepared.prefix_config_hash = "prefix-hash-xyz0"
    fake_prepared.prefix_markdown = "Essay opening text"
    fake_prepared.remainder_markdown = "x" * 2000
    fake_prepared.target_words = 400
    mocker.patch("versus.rumil_completion.prepare.prepare", return_value=fake_prepared)

    fake_project = MagicMock()
    fake_project.id = "project-uuid-12345678"
    fake_project.name = "test-ws"

    probe_db = MagicMock()
    probe_db.list_projects = AsyncMock(return_value=[fake_project])
    mocker.patch(
        "rumil.database.DB.create",
        new=AsyncMock(return_value=probe_db),
    )

    mocker.patch(
        "versus.versus_config.compute_workspace_state_hash",
        new=AsyncMock(return_value="ws-state-hash"),
    )
    mocker.patch(
        "versus.versus_config.compute_judge_code_fingerprint",
        return_value={},
    )
    mocker.patch(
        "versus.model_config.get_model_config",
        return_value=MagicMock(to_record_dict=MagicMock(return_value={})),
    )

    mocker.patch(
        "versus.rumil_completion.versus_db.get_client",
        return_value=MagicMock(),
    )
    # An existing row at the planned (essay, source_id, prefix_hash).
    target_source_id = "orch:two_phase:claude-opus-4-7:cdeadbeef"
    existing = [
        {
            "essay_id": "essay-1",
            "source_id": target_source_id,
            "prefix_hash": "prefix-hash-xyz0",
            "kind": "completion",
        }
    ]
    mocker.patch(
        "versus.rumil_completion.versus_db.iter_texts",
        return_value=iter(existing),
    )

    mocker.patch(
        "versus.versus_config.make_versus_config",
        return_value=({"workflow": "two_phase"}, "deadbeef0123abcd", "fake-judge-model"),
    )

    run_versus_mock = mocker.patch(
        "rumil.versus_runner.run_versus",
        new=AsyncMock(),
    )

    await rumil_completion.run_orch_completion(
        cfg,
        essays=[fake_essay],
        workspace="test-ws",
        workflow_name="two_phase",
        model="claude-opus-4-7",
        budget=4,
        prefix_cfg=prefix_cfg,
    )

    # No pending rows ⇒ no run fires.
    run_versus_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_orch_completion_resolves_workspace_or_exits(mocker):
    cfg = MagicMock()
    cfg.completion.length_tolerance = 0.1
    prefix_cfg = MagicMock()
    prefix_cfg.n_paragraphs = 3
    prefix_cfg.include_headers = True

    fake_project = MagicMock()
    fake_project.id = "p-1"
    fake_project.name = "real-ws"

    probe_db = MagicMock()
    probe_db.list_projects = AsyncMock(return_value=[fake_project])
    mocker.patch(
        "rumil.database.DB.create",
        new=AsyncMock(return_value=probe_db),
    )

    with pytest.raises(SystemExit, match="not found"):
        await rumil_completion.run_orch_completion(
            cfg,
            essays=[],
            workspace="typo-workspace",
            workflow_name="two_phase",
            model="claude-opus-4-7",
            budget=4,
            prefix_cfg=prefix_cfg,
        )
