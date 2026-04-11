"""DB setup, run bookkeeping, and session state for rumil skill scripts.

## Session state

Every rumil-* skill shares a small piece of state scoped to the current
Claude Code session:

  .claude/state/rumil-session.json
    {
      "workspace": "default",
      "chat_envelope": {
        "run_id": "…",
        "call_id": "…",
        "workspace": "default",
        "started_at": "…"
      } | null
    }

The file is gitignored; it lives next to settings.json in the .claude dir
rather than in /tmp so it survives across shells but stays per-worktree.

## Runs

Every skill invocation that runs a call (via dispatch_call or the chat
envelope) creates its own row in the ``runs`` table. The row's ``config``
JSONB carries origin metadata:

    {
      ...settings.capture_config(),
      "origin": "claude-code",
      "skill": "rumil-dispatch",
      "cc_session": "…"        # best-effort, may be null
    }

This lets future analyses split rumil-internal runs from cc-triggered ones
without touching the schema.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rumil.database import DB
from rumil.models import Call, CallType, Workspace
from rumil.settings import get_settings

from ._safety import assert_local_ok

STATE_DIR = Path(".claude/state")
STATE_FILE = STATE_DIR / "rumil-session.json"
DEFAULT_WORKSPACE = "default"


@dataclass
class SessionState:
    workspace: str
    chat_envelope: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "chat_envelope": self.chat_envelope,
        }


def load_session_state() -> SessionState:
    """Load session state, creating a default file if none exists."""
    if not STATE_FILE.exists():
        state = SessionState(workspace=DEFAULT_WORKSPACE)
        save_session_state(state)
        return state
    data = json.loads(STATE_FILE.read_text())
    return SessionState(
        workspace=data.get("workspace") or DEFAULT_WORKSPACE,
        chat_envelope=data.get("chat_envelope"),
    )


def save_session_state(state: SessionState) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state.to_dict(), indent=2))


def resolve_workspace(cli_workspace: str | None) -> str:
    """Return the effective workspace: CLI arg wins over session state."""
    if cli_workspace:
        return cli_workspace
    return load_session_state().workspace


def _cc_session_id() -> str | None:
    """Best-effort Claude Code session identifier for tagging runs.

    Claude Code doesn't currently expose a stable session id to subprocesses,
    so we fall back to the parent-process id as a rough 'this CC window'
    handle. Good enough to group runs from one session together.
    """
    return os.environ.get("CLAUDE_SESSION_ID") or str(os.getppid())


def _git_head() -> str | None:
    """Return the current git HEAD sha (short) or None if not in a repo.

    Captured in run config so later analyses can correlate a run to the
    exact code state that produced it — important when iterating on prompts.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
    except (FileNotFoundError, subprocess.SubprocessError):
        pass
    return None


async def make_db(
    *,
    prod: bool = False,
    staged: bool = False,
    workspace: str | None = None,
    run_id: str | None = None,
) -> tuple[DB, str]:
    """Create a DB scoped to ``workspace`` and return (db, workspace_name).

    Always uses the local Supabase unless ``RUMIL_ALLOW_PROD=1`` is set.
    """
    assert_local_ok(prod)
    ws = resolve_workspace(workspace)
    db = await DB.create(
        run_id=run_id or str(uuid.uuid4()),
        prod=prod,
        staged=staged,
    )
    project = await db.get_or_create_project(ws)
    db.project_id = project.id
    return db, ws


async def open_run(
    db: DB,
    *,
    name: str,
    question_id: str | None,
    skill: str,
    budget: int,
    extra_config: dict[str, Any] | None = None,
) -> None:
    """Insert a ``runs`` row + budget, tagged with origin=claude-code.

    Call this once per skill invocation that dispatches real rumil work.
    After this returns, db.run_id is the anchor for the trace URL.
    """
    await db.init_budget(budget)
    settings = get_settings()
    config: dict[str, Any] = dict(settings.capture_config())
    config.update(
        {
            "origin": "claude-code",
            "skill": skill,
            "cc_session": _cc_session_id(),
            "git_head": _git_head(),
        }
    )
    if extra_config:
        config.update(extra_config)
    await db.create_run(
        name=f"[cc] {name}",
        question_id=question_id,
        config=config,
    )


async def ensure_chat_envelope(
    *,
    scope_question_id: str | None = None,
    workspace: str | None = None,
) -> tuple[DB, Call]:
    """Return (db, envelope_call) for the current CC chat session.

    If no envelope exists in session state, create one. Otherwise, open a DB
    against the envelope's run_id and fetch the existing Call.

    The envelope is a single CLAUDE_CODE_DIRECT Call that owns every
    cc-mediated move made during this chat session. Moves hang off it the
    same way moves hang off a normal rumil call.
    """
    state = load_session_state()
    ws = resolve_workspace(workspace)
    existing = state.chat_envelope
    # An envelope is only reusable if it was created for the *current* session
    # workspace. Otherwise new moves would bleed into a stale project's trace.
    if existing and existing.get("workspace") == ws:
        db = await DB.create(
            run_id=existing["run_id"],
            prod=False,
            staged=False,
        )
        project = await db.get_or_create_project(existing["workspace"])
        db.project_id = project.id
        call = await db.get_call(existing["call_id"])
        if call is not None:
            return db, call
        # Stale pointer — close the leaked client and recreate below.
        await db.close()
        state.chat_envelope = None
    elif existing:
        # Workspace changed out from under the envelope; drop it.
        state.chat_envelope = None
        save_session_state(state)
    db = await DB.create(run_id=str(uuid.uuid4()), prod=False, staged=False)
    project = await db.get_or_create_project(ws)
    db.project_id = project.id

    # A lightweight run row so the envelope call has a trace URL and config.
    # Budget 1 is a placeholder — the envelope doesn't consume it; cc-mediated
    # moves don't go through budget accounting since there's no LLM call.
    await open_run(
        db,
        name="chat envelope",
        question_id=scope_question_id,
        skill="rumil-chat",
        budget=1,
        extra_config={"envelope": True},
    )
    call = await db.create_call(
        CallType.CLAUDE_CODE_DIRECT,
        scope_page_id=scope_question_id,
    )
    # Tag the envelope call so the trace UI (and queries) can distinguish
    # it from a rumil-internal call that happens to have the same type.
    call.call_params = {
        "origin": "claude-code",
        "envelope": True,
        "cc_session": _cc_session_id(),
        "started_at": datetime.now(UTC).isoformat(),
    }
    await db.save_call(call)

    state.chat_envelope = {
        "run_id": db.run_id,
        "call_id": call.id,
        "workspace": ws,
        "started_at": call.call_params["started_at"],
    }
    save_session_state(state)
    return db, call


def clear_chat_envelope() -> None:
    """Forget the active envelope so the next chat invocation starts fresh."""
    state = load_session_state()
    state.chat_envelope = None
    save_session_state(state)
