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


def test_make_workflow_and_task_threads_extra_kwargs():
    """``--workflow-arg`` parsed values reach the workflow constructor
    and become attributes on the instance — pin the wiring so a future
    refactor doesn't drop the kwargs silently."""
    from rumil.orchestrators.draft_and_edit import DraftAndEditWorkflow

    workflow, _ = rumil_completion._make_workflow_and_task(
        "draft_and_edit",
        budget=2,
        extra_kwargs={"n_critics": 3, "max_rounds": 2},
    )
    assert isinstance(workflow, DraftAndEditWorkflow)
    assert workflow.n_critics == 3
    assert workflow.max_rounds == 2


def test_parse_workflow_args_coerces_int_kwargs():
    from rumil.orchestrators.draft_and_edit import DraftAndEditWorkflow

    parsed = rumil_completion._parse_workflow_args(
        ["n_critics=3", "max_rounds=2"], DraftAndEditWorkflow
    )
    assert parsed == {"n_critics": 3, "max_rounds": 2}


def test_parse_workflow_args_keeps_str_kwargs_as_strings():
    from rumil.orchestrators.draft_and_edit import DraftAndEditWorkflow

    parsed = rumil_completion._parse_workflow_args(
        ["drafter_model=claude-sonnet-4-5", "editor_model=claude-opus-4-7"],
        DraftAndEditWorkflow,
    )
    assert parsed == {
        "drafter_model": "claude-sonnet-4-5",
        "editor_model": "claude-opus-4-7",
    }


def test_parse_workflow_args_none_clears_optional_default():
    """``foo=none`` (case-insensitive) maps to Python ``None`` so a
    user can explicitly clear an inherited str-or-None default."""
    from rumil.orchestrators.draft_and_edit import DraftAndEditWorkflow

    parsed = rumil_completion._parse_workflow_args(["drafter_model=None"], DraftAndEditWorkflow)
    assert parsed == {"drafter_model": None}


def test_parse_workflow_args_unknown_key_lists_accepted_names():
    from rumil.orchestrators.draft_and_edit import DraftAndEditWorkflow

    with pytest.raises(ValueError, match="zzz") as excinfo:
        rumil_completion._parse_workflow_args(["zzz=1"], DraftAndEditWorkflow)
    msg = str(excinfo.value)
    assert "n_critics" in msg
    assert "max_rounds" in msg
    assert "drafter_model" in msg


def test_parse_workflow_args_rejects_setting_budget():
    from rumil.orchestrators.draft_and_edit import DraftAndEditWorkflow

    with pytest.raises(ValueError, match="budget"):
        rumil_completion._parse_workflow_args(["budget=10"], DraftAndEditWorkflow)


def test_parse_workflow_args_rejects_malformed_pair():
    from rumil.orchestrators.draft_and_edit import DraftAndEditWorkflow

    with pytest.raises(ValueError, match="key=value"):
        rumil_completion._parse_workflow_args(["bare_token"], DraftAndEditWorkflow)


def test_parse_workflow_args_rejects_non_int_for_int_field():
    from rumil.orchestrators.draft_and_edit import DraftAndEditWorkflow

    with pytest.raises(ValueError, match="expected int"):
        rumil_completion._parse_workflow_args(["n_critics=not-a-number"], DraftAndEditWorkflow)


def test_run_completions_script_advertises_workflow_arg_flag():
    """The CLI exposes --workflow-arg. Pin the flag exists + repeats
    naturally so a refactor that drops it (or stops accumulating
    repeats into a list) trips this test rather than silently degrading
    UX.
    """
    script = Path(__file__).resolve().parents[1] / "versus" / "scripts" / "run_completions.py"
    src = script.read_text()
    # Flag is registered.
    assert '"--workflow-arg"' in src
    # action="append" so repeated flags accumulate (documented behaviour).
    assert 'action="append"' in src
    # metavar advertises the key=value shape.
    assert 'metavar="key=value"' in src
    # Validation routes through the parser in rumil_completion.
    assert "_parse_workflow_args" in src


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
        "versus.versus_config.compute_shared_code_fingerprint",
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
        "versus.versus_config.compute_shared_code_fingerprint",
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


def test_short_model_known_alias_reverses_to_short_form():
    assert rumil_completion.short_model("claude-opus-4-7") == "opus"
    assert rumil_completion.short_model("claude-sonnet-4-6") == "sonnet"
    assert rumil_completion.short_model("claude-haiku-4-5-20251001") == "haiku"


def test_short_model_namespaced_id_strips_provider_prefix():
    """OpenRouter-style ``provider/model`` ids resolve to the alias when
    the bare segment is registered, else fall back to the bare segment."""
    assert rumil_completion.short_model("anthropic/claude-opus-4-7") == "opus"
    assert rumil_completion.short_model("openai/gpt-5") == "gpt-5"


def test_short_model_unknown_id_falls_back_to_bare():
    assert rumil_completion.short_model("gpt-5") == "gpt-5"


@pytest.mark.asyncio
async def test_run_orch_completion_run_name_carries_differentiators(mocker):
    """Gap 2 regression: the run name passed to ``db.create_run`` must
    include workspace, essay_id, prefix label, workflow, model alias, and
    budget so the traces list disambiguates runs at a glance.
    """
    cfg = MagicMock()
    cfg.completion.length_tolerance = 0.1

    from versus import config as versus_config

    prefix_cfg = versus_config.PrefixCfg(id="no_headers", n_paragraphs=3, include_headers=False)

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
    fake_project.name = "ws-display"

    probe_db = MagicMock()
    probe_db.list_projects = AsyncMock(return_value=[fake_project])
    per_run_db = MagicMock()
    per_run_db.create_run = AsyncMock()

    create_mock = AsyncMock(side_effect=[probe_db, per_run_db])
    mocker.patch("rumil.database.DB.create", new=create_mock)

    mocker.patch(
        "versus.versus_config.compute_workspace_state_hash",
        new=AsyncMock(return_value="ws-state-hash"),
    )
    mocker.patch(
        "versus.versus_config.compute_shared_code_fingerprint",
        return_value={},
    )
    mocker.patch(
        "versus.model_config.get_model_config",
        return_value=MagicMock(to_record_dict=MagicMock(return_value={"temperature": 1.0})),
    )
    mocker.patch(
        "versus.rumil_completion.versus_db.get_client",
        return_value=MagicMock(),
    )
    mocker.patch(
        "versus.rumil_completion.versus_db.iter_texts",
        return_value=iter([]),
    )
    mocker.patch(
        "versus.rumil_completion.versus_db.insert_text",
    )
    mocker.patch(
        "versus.versus_config.make_versus_config",
        return_value=({"workflow": "two_phase"}, "deadbeef0123abcd", "fake-judge-model"),
    )

    fake_result = MagicMock()
    fake_result.artifact.text = "ARTIFACT TEXT"
    fake_result.artifact.raw_response = "RAW"
    fake_result.call_id = "call-1"
    fake_result.question_id = "q-1"
    fake_result.cost_usd = 0.01
    fake_result.trace_url = "http://x/traces/1"
    fake_result.status = "completed"
    mocker.patch(
        "rumil.versus_runner.run_versus",
        new=AsyncMock(return_value=fake_result),
    )

    await rumil_completion.run_orch_completion(
        cfg,
        essays=[fake_essay],
        workspace="ws-display",
        workflow_name="two_phase",
        model="claude-sonnet-4-6",
        budget=7,
        prefix_cfg=prefix_cfg,
    )

    per_run_db.create_run.assert_awaited_once()
    kwargs = per_run_db.create_run.await_args.kwargs
    name = kwargs["name"]
    assert name.startswith("versus-orch-completion:")
    assert "ws-display" in name
    assert "essay-1" in name
    assert "@no_headers" in name
    assert "two_phase" in name
    assert "/sonnet/" in name
    assert "/b7" in name


@pytest.mark.asyncio
async def test_run_orch_judge_run_name_carries_differentiators(mocker):
    """Gap 2 regression on the judge path: the run name must include
    workspace, essay_id, prefix label, workflow, model alias, budget,
    and dimension."""
    from versus import config as versus_config
    from versus import rumil_judge

    cfg = MagicMock()
    cfg.judging.include_human_as_contestant = True

    prefix_cfg = versus_config.PrefixCfg(id="default", n_paragraphs=3, include_headers=True)

    fake_project = MagicMock()
    fake_project.id = "project-uuid-12345678"
    fake_project.name = "ws-display"
    probe_db = MagicMock()
    probe_db.list_projects = AsyncMock(return_value=[fake_project])
    per_run_db = MagicMock()
    per_run_db.create_run = AsyncMock()
    create_mock = AsyncMock(side_effect=[probe_db, per_run_db])
    mocker.patch("rumil.database.DB.create", new=create_mock)

    mocker.patch(
        "versus.versus_config.compute_workspace_state_hash",
        new=AsyncMock(return_value="ws-state-hash"),
    )
    mocker.patch("rumil.versus_bridge.compute_orch_closer_hash", return_value="closerhh")
    mocker.patch("rumil.versus_bridge.compute_pair_surface_hash", return_value="pairsurf")
    mocker.patch("rumil.versus_bridge.compute_prompt_hash", return_value="prompthh")
    mocker.patch("rumil.versus_bridge.compute_tool_prompt_hash", return_value="toolhash")
    mocker.patch("rumil.versus_bridge.get_rumil_dimension_body", return_value="dimbody")
    mocker.patch(
        "versus.model_config.get_judge_model_config",
        return_value=MagicMock(to_record_dict=MagicMock(return_value={})),
    )
    mocker.patch(
        "versus.versus_config.make_judge_config",
        return_value=({"judge": "config"}, "ihash", "rumil:orch:m:d:c01"),
    )

    fake_pair = rumil_judge._PendingPair(
        essay_id="essay-1",
        prefix_hash="prefix-hash",
        prefix_text="prefix",
        source_a_id="human",
        source_a_text="A",
        source_a_text_id="ta",
        source_b_id="orch:two_phase:m:cdeadbeef",
        source_b_text="B",
        source_b_text_id="tb",
        display_first_id="human",
        display_first_text="A",
        display_second_id="orch:two_phase:m:cdeadbeef",
        display_second_text="B",
    )
    fake_pj = rumil_judge._PendingJudgment(
        pair=fake_pair,
        task_name="general_quality",
        is_versus_crit=False,
        judge_model="rumil:orch:m:d:c01",
        base_config={"judge": "config"},
    )
    mocker.patch(
        "versus.rumil_judge._plan_rumil_pairs",
        return_value=[fake_pj],
    )
    mocker.patch(
        "versus.rumil_judge.versus_db.get_client",
        return_value=MagicMock(),
    )
    mocker.patch(
        "versus.rumil_judge.versus_db.insert_judgment",
    )

    fake_result = MagicMock()
    fake_result.verdict = "A"
    fake_result.preference_label = "A somewhat preferred"
    fake_result.reasoning_text = "..."
    fake_result.call_id = "c1"
    fake_result.run_id = "r1"
    fake_result.question_id = "q1"
    fake_result.cost_usd = 0.05
    fake_result.trace_url = "http://x/traces/r1"
    mocker.patch(
        "rumil.versus_bridge.judge_pair_orch",
        new=AsyncMock(return_value=fake_result),
    )

    await rumil_judge.run_orch(
        cfg,
        workspace="ws-display",
        model="claude-opus-4-7",
        dimensions=("general_quality",),
        budget=4,
        prefix_cfg=prefix_cfg,
    )

    per_run_db.create_run.assert_awaited_once()
    name = per_run_db.create_run.await_args.kwargs["name"]
    assert name.startswith("versus-orch-judge:")
    assert "ws-display" in name
    assert "essay-1" in name
    assert "@default" in name
    assert "two_phase" in name
    assert "/opus/" in name
    assert "/b4/" in name
    assert "general_quality" in name
