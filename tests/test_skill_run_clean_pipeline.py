"""Smoke test for rumil_skills.run_clean_pipeline.

Seeds an evaluate call with a minimal evaluation text, then invokes the
clean pipeline as a subprocess (via `uv run python -m
rumil_skills.run_clean_pipeline`). Asserts structurally — exit 0, run
row created with origin=claude-code, a grounding_feedback call exists.
"""

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
import pytest_asyncio

from rumil.database import DB
from rumil.models import (
    Call,
    CallStatus,
    CallType,
    Page,
    PageLayer,
    PageType,
    Workspace,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest_asyncio.fixture
async def seeded_eval_workspace():
    """Create a scratch workspace with a question + a completed evaluate call."""
    ws_name = f"test-skill-clean-{uuid.uuid4().hex[:6]}"
    run_id = str(uuid.uuid4())
    db = await DB.create(run_id=run_id, staged=False)
    project = await db.get_or_create_project(ws_name)
    db.project_id = project.id
    await db.init_budget(5)
    await db.create_run(
        name="seed run",
        question_id=None,
        config={"origin": "test-seed"},
    )

    question = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Is the sky blue?",
        headline="Is the sky blue?",
    )
    await db.save_page(question)

    eval_call = Call(
        call_type=CallType.EVALUATE,
        workspace=Workspace.RESEARCH,
        scope_page_id=question.id,
        status=CallStatus.COMPLETE,
        review_json={
            "evaluation": "The research is adequate. No further grounding needed.",
        },
        result_summary="No actionable gaps identified.",
    )
    await db.save_call(eval_call)

    yield {
        "workspace": ws_name,
        "question_id": question.id,
        "eval_call_id": eval_call.id,
        "db": db,
        "project_id": project.id,
    }

    try:
        resp = await db._execute(db.client.table("runs").select("id").eq("project_id", project.id))
        run_ids = [r["id"] for r in (getattr(resp, "data", None) or []) if r["id"] != db.run_id]
        for rid in run_ids:
            cleanup_db = await DB.create(run_id=rid, staged=False)
            cleanup_db.project_id = project.id
            await cleanup_db.delete_run_data(delete_project=False)
            await cleanup_db.close()
        await db.delete_run_data(delete_project=True)
    finally:
        await db.close()


@pytest.mark.integration
async def test_run_clean_pipeline_grounding_subprocess(seeded_eval_workspace):
    """Invoke run_clean_pipeline as a subprocess with grounding mode."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / ".claude" / "lib")
    env["RUMIL_TEST_MODE"] = "1"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "rumil_skills.run_clean_pipeline",
            "grounding",
            seeded_eval_workspace["eval_call_id"],
            "--workspace",
            seeded_eval_workspace["workspace"],
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert result.returncode == 0, (
        f"subprocess failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )

    db = seeded_eval_workspace["db"]
    runs_resp = await db._execute(
        db.client.table("runs").select("*").eq("project_id", db.project_id)
    )
    runs = list(getattr(runs_resp, "data", None) or [])
    cc_runs = [r for r in runs if (r.get("config") or {}).get("origin") == "claude-code"]
    assert len(cc_runs) >= 1
    assert any((r.get("config") or {}).get("skill") == "rumil-clean" for r in cc_runs)
    assert any((r.get("config") or {}).get("pipeline") == "grounding" for r in cc_runs)

    calls_resp = await db._execute(
        db.client.table("calls")
        .select("id,call_type,status,run_id")
        .eq("project_id", db.project_id)
        .eq("call_type", CallType.GROUNDING_FEEDBACK.value)
    )
    calls = list(getattr(calls_resp, "data", None) or [])
    assert len(calls) >= 1


@pytest.mark.integration
async def test_run_clean_pipeline_rejects_non_evaluate_call(seeded_eval_workspace):
    """Passing a non-evaluate call id exits with a descriptive error."""
    db = seeded_eval_workspace["db"]
    bad_call = Call(
        call_type=CallType.FIND_CONSIDERATIONS,
        workspace=Workspace.RESEARCH,
        scope_page_id=seeded_eval_workspace["question_id"],
        status=CallStatus.COMPLETE,
    )
    await db.save_call(bad_call)

    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / ".claude" / "lib")
    env["RUMIL_TEST_MODE"] = "1"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "rumil_skills.run_clean_pipeline",
            "grounding",
            bad_call.id,
            "--workspace",
            seeded_eval_workspace["workspace"],
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 1
    assert "not an evaluate call" in result.stderr
