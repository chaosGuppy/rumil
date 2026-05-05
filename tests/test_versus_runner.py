"""Tests for ``rumil.versus_runner.run_versus``.

Heavily mocked: we exercise the orchestration glue (workflow + task
phases run in order, artifact extracted from closer text, cost summed
across run calls), not the underlying SDK / orchestrator behavior.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_VERSUS_SRC = Path(__file__).resolve().parents[1] / "versus" / "src"
if str(_VERSUS_SRC) not in sys.path:
    sys.path.insert(0, str(_VERSUS_SRC))

from rumil.models import CallType  # noqa: E402
from rumil.versus_runner import VersusResult, run_versus  # noqa: E402


@dataclass
class _Artifact:
    label: str


def _make_workflow(mocker, *, produces_artifact: bool = False):
    workflow = MagicMock()
    workflow.name = "test_workflow"
    workflow.produces_artifact = produces_artifact
    workflow.fingerprint = MagicMock(return_value={"kind": "test_workflow"})
    workflow.setup = AsyncMock()
    workflow.run = AsyncMock()
    del workflow.last_status  # opt-in attr; tests set it explicitly when needed
    return workflow


def _make_task(mocker, *, system="SYS", user="USR"):
    task = MagicMock()
    task.name = "test_task"
    task.create_question = AsyncMock(return_value="q-1")
    task.render_for_closer = AsyncMock(return_value="RENDERED")
    task.closer_prompts = MagicMock(return_value=(system, user))
    task.extract_artifact = MagicMock(return_value=_Artifact(label="A"))
    task.sdk_max_turns = 5
    task.disallowed_tools = ("Write",)
    task.tool_server_name = "test-server"
    task.call_type = CallType.VERSUS_JUDGE
    return task


def _make_db(mocker):
    db = MagicMock()
    db.run_id = "run-1"
    db.get_calls_for_run = AsyncMock(return_value=[])
    return db


@pytest.mark.asyncio
async def test_run_versus_runs_phases_in_order(mocker):
    """create_question -> setup -> workflow.run -> render_for_closer ->
    closer_prompts -> closer_agent -> extract_artifact, in that order.
    """
    workflow = _make_workflow(mocker)
    task = _make_task(mocker)
    db = _make_db(mocker)
    fake_call = MagicMock()
    fake_call.id = "call-1"
    closer = mocker.patch(
        "rumil.versus_runner.run_closer_agent",
        new=AsyncMock(return_value=("CLOSER_TEXT", fake_call)),
    )
    result = await run_versus(
        db,
        workflow=workflow,
        task=task,
        inputs={"x": 1},
        model="claude-haiku-4-5",
    )
    assert isinstance(result, VersusResult)
    task.create_question.assert_awaited_once_with(db, {"x": 1})
    workflow.setup.assert_awaited_once_with(db, "q-1")
    workflow.run.assert_awaited_once_with(db, "q-1", None, model_config=None)
    task.render_for_closer.assert_awaited_once_with(db, "q-1")
    task.closer_prompts.assert_called_once_with("RENDERED", {"x": 1})
    closer.assert_awaited_once()
    task.extract_artifact.assert_called_once_with("CLOSER_TEXT")


@pytest.mark.asyncio
async def test_run_versus_returns_artifact_and_metadata(mocker):
    workflow = _make_workflow(mocker)
    task = _make_task(mocker, system="SYSTEM_SENTINEL", user="USER_SENTINEL")
    db = _make_db(mocker)
    fake_call = MagicMock()
    fake_call.id = "call-2"
    mocker.patch(
        "rumil.versus_runner.run_closer_agent",
        new=AsyncMock(return_value=("text", fake_call)),
    )
    result = await run_versus(
        db,
        workflow=workflow,
        task=task,
        inputs=None,
        model="claude-haiku-4-5",
    )
    assert result.artifact == _Artifact(label="A")
    assert result.run_id == "run-1"
    assert result.call_id == "call-2"
    assert result.question_id == "q-1"
    assert result.system_prompt == "SYSTEM_SENTINEL"
    assert result.user_prompt == "USER_SENTINEL"
    assert "/traces/run-1" in result.trace_url
    assert result.status == "complete"


@pytest.mark.asyncio
async def test_run_versus_threads_workflow_last_status(mocker):
    """Workflows that produce partial output (e.g. DraftAndEdit running
    out of budget mid-edit) signal it by setting ``last_status``; the
    runner threads that onto the result.
    """
    workflow = _make_workflow(mocker)
    workflow.last_status = "incomplete"
    task = _make_task(mocker)
    db = _make_db(mocker)
    fake_call = MagicMock()
    fake_call.id = "call-1"
    mocker.patch(
        "rumil.versus_runner.run_closer_agent",
        new=AsyncMock(return_value=("text", fake_call)),
    )
    result = await run_versus(
        db,
        workflow=workflow,
        task=task,
        inputs=None,
        model="claude-haiku-4-5",
    )
    assert result.status == "incomplete"


@pytest.mark.asyncio
async def test_run_versus_sums_cost_across_run_calls(mocker):
    """Cost reported is the sum of every call recorded under the run id —
    workflow dispatches plus the closer."""
    workflow = _make_workflow(mocker)
    task = _make_task(mocker)
    db = _make_db(mocker)
    c1, c2, c3 = MagicMock(), MagicMock(), MagicMock()
    c1.cost_usd = 0.01
    c2.cost_usd = 0.02
    c3.cost_usd = None  # cost may be missing on a call; treated as 0.
    db.get_calls_for_run = AsyncMock(return_value=[c1, c2, c3])
    fake_call = MagicMock()
    fake_call.id = "call-1"
    mocker.patch(
        "rumil.versus_runner.run_closer_agent",
        new=AsyncMock(return_value=("text", fake_call)),
    )
    result = await run_versus(
        db,
        workflow=workflow,
        task=task,
        inputs=None,
        model="claude-haiku-4-5",
    )
    assert result.cost_usd == pytest.approx(0.03)


@pytest.mark.asyncio
async def test_run_versus_threads_model_into_settings_override(mocker):
    """The `model` arg becomes ``rumil_model_override`` for the duration
    of the run — orchestrator's nested LLM calls all see it. Verify by
    sniffing get_settings() inside the workflow.run mock.
    """
    from rumil.settings import get_settings

    captured = {}

    async def _spy_run(db, qid, broadcaster, *, model_config=None):
        captured["model_override"] = get_settings().rumil_model_override

    workflow = _make_workflow(mocker)
    workflow.run = AsyncMock(side_effect=_spy_run)
    task = _make_task(mocker)
    db = _make_db(mocker)
    fake_call = MagicMock()
    fake_call.id = "call-1"
    mocker.patch(
        "rumil.versus_runner.run_closer_agent",
        new=AsyncMock(return_value=("text", fake_call)),
    )
    await run_versus(
        db,
        workflow=workflow,
        task=task,
        inputs=None,
        model="claude-haiku-4-5",
    )
    assert captured["model_override"] == "claude-haiku-4-5"


@pytest.mark.asyncio
async def test_run_versus_skips_closer_when_workflow_produces_artifact(mocker):
    """``produces_artifact=True`` workflows have already written the
    final text to ``question.content``; the runner reads it directly
    and skips the closer call. ``extract_artifact`` runs against that
    text; ``system_prompt`` / ``user_prompt`` come back empty.
    """
    workflow = _make_workflow(mocker, produces_artifact=True)
    task = _make_task(mocker)
    db = _make_db(mocker)
    fake_question = MagicMock()
    fake_question.content = "FINAL_ARTIFACT_TEXT"
    db.get_page = AsyncMock(return_value=fake_question)
    fake_call = MagicMock()
    fake_call.id = "wf-call-1"
    fake_call.created_at = 1.0
    db.get_calls_for_run = AsyncMock(return_value=[fake_call])
    closer = mocker.patch(
        "rumil.versus_runner.run_closer_agent",
        new=AsyncMock(return_value=("UNUSED", MagicMock())),
    )
    result = await run_versus(
        db,
        workflow=workflow,
        task=task,
        inputs={"x": 1},
        model="claude-haiku-4-5",
    )
    closer.assert_not_awaited()
    task.render_for_closer.assert_not_awaited()
    task.closer_prompts.assert_not_called()
    task.extract_artifact.assert_called_once_with("FINAL_ARTIFACT_TEXT")
    assert result.system_prompt == ""
    assert result.user_prompt == ""
    # call_id falls back to the most-recent run call.
    assert result.call_id == "wf-call-1"


@pytest.mark.asyncio
async def test_run_versus_raises_when_artifact_workflow_question_missing(mocker):
    workflow = _make_workflow(mocker, produces_artifact=True)
    task = _make_task(mocker)
    db = _make_db(mocker)
    db.get_page = AsyncMock(return_value=None)
    with pytest.raises(RuntimeError, match="produces_artifact"):
        await run_versus(
            db,
            workflow=workflow,
            task=task,
            inputs=None,
            model="claude-haiku-4-5",
        )


@pytest.mark.asyncio
async def test_run_versus_threads_task_closer_overrides_into_helper(mocker):
    """JudgePairTask exposes ``sdk_max_turns`` / ``disallowed_tools`` /
    ``tool_server_name`` attrs; the runner reads them and forwards to
    ``run_closer_agent``. Verifies the wiring without coupling to the
    helper's internal structure.
    """
    workflow = _make_workflow(mocker)
    task = _make_task(mocker)
    task.sdk_max_turns = 9
    task.disallowed_tools = ("X", "Y")
    task.tool_server_name = "my-server"
    db = _make_db(mocker)
    fake_call = MagicMock()
    fake_call.id = "call-1"
    closer = mocker.patch(
        "rumil.versus_runner.run_closer_agent",
        new=AsyncMock(return_value=("text", fake_call)),
    )
    await run_versus(
        db,
        workflow=workflow,
        task=task,
        inputs=None,
        model="claude-haiku-4-5",
    )
    kwargs = closer.call_args.kwargs
    assert kwargs["max_turns"] == 9
    assert kwargs["disallowed_tools"] == ("X", "Y")
    assert kwargs["server_name"] == "my-server"


@pytest.mark.asyncio
async def test_run_versus_forwards_task_call_type_to_closer(mocker):
    """The runner is task-agnostic on call_type: judge tasks land as
    VERSUS_JUDGE, completion tasks as VERSUS_COMPLETE. Verifies the
    attribute propagates rather than the runner picking a default."""
    workflow = _make_workflow(mocker)
    task = _make_task(mocker)
    task.call_type = CallType.VERSUS_COMPLETE
    db = _make_db(mocker)
    fake_call = MagicMock()
    fake_call.id = "call-1"
    closer = mocker.patch(
        "rumil.versus_runner.run_closer_agent",
        new=AsyncMock(return_value=("text", fake_call)),
    )
    await run_versus(
        db,
        workflow=workflow,
        task=task,
        inputs=None,
        model="claude-haiku-4-5",
    )
    assert closer.call_args.kwargs["call_type"] == CallType.VERSUS_COMPLETE
