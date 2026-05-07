"""Smoke test for rumil_skills.run_orchestrator.

Runs the actual subprocess (via `uv run python -m rumil_skills.run_orchestrator`)
against a seeded workspace with a tiny budget and --smoke-test. Asserts
structurally — exit 0, run row created with origin=claude-code, at least
one call exists.
"""

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
import pytest_asyncio

from rumil.database import DB
from rumil.models import Page, PageLayer, PageType, Workspace

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest_asyncio.fixture
async def seeded_workspace():
    """Create a unique scratch workspace with one seeded question. Cleans up after."""
    ws_name = f"test-skill-orch-{uuid.uuid4().hex[:6]}"
    run_id = str(uuid.uuid4())
    db = await DB.create(run_id=run_id, staged=False)
    project = await db.get_or_create_project(ws_name)
    db.project_id = project.id
    await db.init_budget(5)
    question = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Is the sky blue because of Rayleigh scattering?",
        headline="Why is the sky blue?",
    )
    await db.save_page(question)

    yield {
        "workspace": ws_name,
        "question_id": question.id,
        "db": db,
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
async def test_run_orchestrator_subprocess_completes(seeded_workspace):
    """Invoke the run_orchestrator module as a subprocess, check exit status and DB state."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / ".claude" / "lib")
    env["RUMIL_TEST_MODE"] = "1"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "rumil_skills.run_orchestrator",
            seeded_workspace["question_id"],
            "--workspace",
            seeded_workspace["workspace"],
            "--orchestrator",
            "experimental",
            "--budget",
            "4",
            "--smoke-test",
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

    db = seeded_workspace["db"]
    runs_resp = await db._execute(
        db.client.table("runs").select("*").eq("project_id", db.project_id)
    )
    runs = list(getattr(runs_resp, "data", None) or [])
    assert len(runs) >= 1

    cc_runs = [r for r in runs if (r.get("config") or {}).get("origin") == "claude-code"]
    assert len(cc_runs) >= 1
    assert any((r.get("config") or {}).get("skill") == "rumil-orchestrate" for r in cc_runs)

    calls_resp = await db._execute(
        db.client.table("calls")
        .select("id,call_type,status,run_id")
        .eq("project_id", db.project_id)
    )
    calls = list(getattr(calls_resp, "data", None) or [])
    assert len(calls) >= 1
