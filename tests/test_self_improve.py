"""Tests for self-improvement analysis and its CLI wiring.

Covers:
- Pure helpers (_truncate, _safe_repo_path, _is_full_uuid,
  _format_call_summary)
- Subtree collection + call gathering (cycle-safe)
- Each tool exposed to the LLM (overview, read_page,
  list_pages_for_call, get_call_details, get_llm_exchange,
  read_repo_file, list_repo_dir) — including the regression that
  get_llm_exchange must reject short prefixes cleanly and
  get_call_details must print full exchange UUIDs
- save_self_improvement file naming/writing
- run_self_improvement precondition checks
- --self-improve CLI flag: parsed, shown in --help, dispatched
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest

import rumil.self_improve as si
from rumil.models import (
    Call,
    CallStatus,
    CallType,
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)
from rumil.self_improve import (
    _build_tools,
    _collect_subtree,
    _fetch_subtree_calls,
    _format_call_summary,
    _is_full_uuid,
    _safe_repo_path,
    _truncate,
    run_self_improvement,
    save_self_improvement,
)


def _get_tool(tools, name):
    return next(t for t in tools if t.name == name)


def test_truncate_leaves_short_text_untouched():
    assert _truncate("short", 100) == "short"


def test_truncate_elides_long_text_with_marker():
    long = "x" * 200
    out = _truncate(long, 50)
    assert out.startswith("x" * 50)
    assert "truncated" in out
    assert "150 more chars" in out


def test_is_full_uuid_accepts_canonical_uuid():
    assert _is_full_uuid("550e8400-e29b-41d4-a716-446655440000") is True


@pytest.mark.parametrize(
    "bad",
    ["", "4c493126", "not-a-uuid", "550e8400", "550e8400-e29b-41d4-a716"],
)
def test_is_full_uuid_rejects_non_uuids(bad):
    assert _is_full_uuid(bad) is False


def test_safe_repo_path_accepts_repo_file():
    out = _safe_repo_path("README.md")
    assert out is not None
    assert out.name == "README.md"


@pytest.mark.parametrize(
    "bad",
    ["", "/etc/passwd", "../../etc/passwd", "../../../tmp/anything"],
)
def test_safe_repo_path_rejects_escapes(bad):
    assert _safe_repo_path(bad) is None


def test_format_call_summary_includes_key_fields():
    call = Call(
        call_type=CallType.FIND_CONSIDERATIONS,
        workspace=Workspace.RESEARCH,
        scope_page_id="abc12345-0000-0000-0000-000000000000",
        status=CallStatus.COMPLETE,
        budget_used=2,
        cost_usd=0.123,
    )
    out = _format_call_summary(call)
    assert "find_considerations" in out
    assert "status=complete" in out
    assert "budget_used=2" in out
    assert "$0.123" in out
    assert call.id[:8] in out


def test_format_call_summary_handles_missing_cost_and_scope():
    call = Call(
        call_type=CallType.ASSESS,
        workspace=Workspace.RESEARCH,
        scope_page_id=None,
        status=CallStatus.PENDING,
    )
    out = _format_call_summary(call)
    assert "scope=none" in out
    assert "cost=?" in out


async def test_collect_subtree_walks_children(tmp_db, question_page):
    c1 = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="c1",
        headline="Child question 1",
    )
    c2 = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="c2",
        headline="Child question 2",
    )
    await tmp_db.save_page(c1)
    await tmp_db.save_page(c2)
    await tmp_db.save_link(
        PageLink(
            from_page_id=question_page.id,
            to_page_id=c1.id,
            link_type=LinkType.CHILD_QUESTION,
        )
    )
    await tmp_db.save_link(
        PageLink(
            from_page_id=question_page.id,
            to_page_id=c2.id,
            link_type=LinkType.CHILD_QUESTION,
        )
    )
    result = await _collect_subtree(question_page.id, tmp_db)
    ids = {p.id for p, _ in result}
    assert question_page.id in ids
    assert c1.id in ids
    assert c2.id in ids
    root_entries = [entry for entry in result if entry[1] == 0]
    assert len(root_entries) == 1
    assert root_entries[0][0].id == question_page.id


async def test_collect_subtree_terminates_on_cycles(tmp_db, question_page):
    child = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="cyclic",
        headline="Cyclic child",
    )
    await tmp_db.save_page(child)
    await tmp_db.save_link(
        PageLink(
            from_page_id=question_page.id,
            to_page_id=child.id,
            link_type=LinkType.CHILD_QUESTION,
        )
    )
    await tmp_db.save_link(
        PageLink(
            from_page_id=child.id,
            to_page_id=question_page.id,
            link_type=LinkType.CHILD_QUESTION,
        )
    )
    result = await _collect_subtree(question_page.id, tmp_db)
    ids = [p.id for p, _ in result]
    assert len(ids) == len(set(ids))
    assert set(ids) == {question_page.id, child.id}


async def test_collect_subtree_missing_root_returns_empty(tmp_db):
    fake_id = "00000000-0000-0000-0000-000000000000"
    assert list(await _collect_subtree(fake_id, tmp_db)) == []


async def test_fetch_subtree_calls_returns_scoped_calls(
    tmp_db,
    question_page,
    scout_call,
):
    other_question = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="other",
        headline="Other question",
    )
    await tmp_db.save_page(other_question)
    other_call = Call(
        call_type=CallType.ASSESS,
        workspace=Workspace.RESEARCH,
        scope_page_id=other_question.id,
        status=CallStatus.PENDING,
    )
    await tmp_db.save_call(other_call)

    result = await _fetch_subtree_calls([question_page.id], tmp_db)
    ids = {c.id for c in result}
    assert scout_call.id in ids
    assert other_call.id not in ids


async def test_fetch_subtree_calls_empty_input_short_circuits(tmp_db):
    assert list(await _fetch_subtree_calls([], tmp_db)) == []


async def test_overview_lists_root_subtree_and_calls(
    tmp_db,
    question_page,
    scout_call,
):
    tools = _build_tools(
        question_page.id,
        [(question_page, 0)],
        [scout_call],
        tmp_db,
    )
    out = await _get_tool(tools, "get_investigation_overview").fn({})
    assert question_page.headline in out
    assert question_page.id[:8] in out
    assert scout_call.id[:8] in out
    assert "find_considerations" in out


async def test_overview_counts_pages_produced_by_calls(
    tmp_db,
    question_page,
    scout_call,
):
    claim = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="c",
        headline="A claim produced by the scout",
        provenance_call_id=scout_call.id,
    )
    await tmp_db.save_page(claim)
    tools = _build_tools(
        question_page.id,
        [(question_page, 0)],
        [scout_call],
        tmp_db,
    )
    out = await _get_tool(tools, "get_investigation_overview").fn({})
    assert "claim" in out.lower()
    assert "1" in out


async def test_read_page_accepts_short_id(tmp_db, question_page):
    tools = _build_tools(question_page.id, [(question_page, 0)], [], tmp_db)
    out = await _get_tool(tools, "read_page").fn({"page_id": question_page.id[:8]})
    assert question_page.headline in out
    assert "question" in out.lower()


async def test_read_page_accepts_full_id(tmp_db, question_page):
    tools = _build_tools(question_page.id, [(question_page, 0)], [], tmp_db)
    out = await _get_tool(tools, "read_page").fn({"page_id": question_page.id})
    assert question_page.headline in out


async def test_read_page_missing_id_returns_error(tmp_db, question_page):
    tools = _build_tools(question_page.id, [(question_page, 0)], [], tmp_db)
    out = await _get_tool(tools, "read_page").fn(
        {"page_id": "zzzzzzzz-0000-0000-0000-000000000000"}
    )
    assert "not found" in out.lower()


async def test_list_pages_for_call_returns_created_pages(
    tmp_db,
    question_page,
    scout_call,
):
    claim = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="c",
        headline="A discovered claim",
        provenance_call_id=scout_call.id,
        credence=7,
        robustness=3,
    )
    await tmp_db.save_page(claim)
    tools = _build_tools(
        question_page.id,
        [(question_page, 0)],
        [scout_call],
        tmp_db,
    )
    out = await _get_tool(tools, "list_pages_for_call").fn({"call_id": scout_call.id[:8]})
    assert "A discovered claim" in out
    assert claim.id[:8] in out
    assert "C7" in out
    assert "R3" in out


async def test_list_pages_for_call_no_results(
    tmp_db,
    question_page,
    scout_call,
):
    tools = _build_tools(
        question_page.id,
        [(question_page, 0)],
        [scout_call],
        tmp_db,
    )
    out = await _get_tool(tools, "list_pages_for_call").fn({"call_id": scout_call.id})
    assert "No pages" in out


async def test_get_call_details_prints_full_exchange_uuid(
    tmp_db,
    question_page,
    scout_call,
):
    """Regression: the LLM passes exchange ids it sees in get_call_details
    directly to get_llm_exchange, which requires full UUIDs (the column is
    uuid-typed, so LIKE prefix-matching doesn't work). If we showed only
    a short prefix here, the next tool call would crash."""
    exchange_id = await tmp_db.save_llm_exchange(
        call_id=scout_call.id,
        phase="build_context",
        system_prompt="sys",
        user_message="usr",
        response_text="resp",
        round_num=0,
    )
    tools = _build_tools(
        question_page.id,
        [(question_page, 0)],
        [scout_call],
        tmp_db,
    )
    out = await _get_tool(tools, "get_call_details").fn({"call_id": scout_call.id})
    assert exchange_id in out


async def test_get_call_details_includes_core_metadata(
    tmp_db,
    question_page,
    scout_call,
):
    tools = _build_tools(
        question_page.id,
        [(question_page, 0)],
        [scout_call],
        tmp_db,
    )
    out = await _get_tool(tools, "get_call_details").fn({"call_id": scout_call.id})
    assert scout_call.id in out
    assert scout_call.call_type.value in out
    assert scout_call.status.value in out


async def test_get_call_details_missing_call_returns_error(
    tmp_db,
    question_page,
):
    tools = _build_tools(question_page.id, [(question_page, 0)], [], tmp_db)
    out = await _get_tool(tools, "get_call_details").fn({"call_id": "zzzzzzzz"})
    assert "not found" in out.lower()


async def test_get_llm_exchange_rejects_short_prefix(
    tmp_db,
    question_page,
    scout_call,
):
    """Regression: previously a short prefix like '4c493126' reached the DB
    and raised a postgres uuid cast error. Now it must be rejected in
    Python with a helpful message pointing at get_call_details."""
    tools = _build_tools(
        question_page.id,
        [(question_page, 0)],
        [scout_call],
        tmp_db,
    )
    out = await _get_tool(tools, "get_llm_exchange").fn({"exchange_id": "4c493126"})
    assert "not a full UUID" in out
    assert "get_call_details" in out


async def test_get_llm_exchange_reads_full_exchange(
    tmp_db,
    question_page,
    scout_call,
):
    exchange_id = await tmp_db.save_llm_exchange(
        call_id=scout_call.id,
        phase="update_workspace",
        system_prompt="YOU-ARE-A-TEST-AGENT",
        user_message="DO-THE-WORK",
        response_text="HERE-IS-MY-RESPONSE",
        round_num=1,
    )
    tools = _build_tools(
        question_page.id,
        [(question_page, 0)],
        [scout_call],
        tmp_db,
    )
    out = await _get_tool(tools, "get_llm_exchange").fn({"exchange_id": exchange_id})
    assert "YOU-ARE-A-TEST-AGENT" in out
    assert "DO-THE-WORK" in out
    assert "HERE-IS-MY-RESPONSE" in out


async def test_get_llm_exchange_empty_input_returns_error(
    tmp_db,
    question_page,
):
    tools = _build_tools(question_page.id, [(question_page, 0)], [], tmp_db)
    out = await _get_tool(tools, "get_llm_exchange").fn({"exchange_id": ""})
    assert "required" in out.lower()


async def test_get_llm_exchange_unknown_full_uuid_returns_not_found(
    tmp_db,
    question_page,
):
    tools = _build_tools(question_page.id, [(question_page, 0)], [], tmp_db)
    fake = "00000000-0000-0000-0000-000000000000"
    out = await _get_tool(tools, "get_llm_exchange").fn({"exchange_id": fake})
    assert "not found" in out.lower()


async def test_read_repo_file_reads_real_file(tmp_db, question_page):
    tools = _build_tools(question_page.id, [(question_page, 0)], [], tmp_db)
    out = await _get_tool(tools, "read_repo_file").fn({"path": "README.md"})
    assert "rumil" in out.lower() or "research" in out.lower()


async def test_read_repo_file_reads_prompt_file(tmp_db, question_page):
    tools = _build_tools(question_page.id, [(question_page, 0)], [], tmp_db)
    out = await _get_tool(tools, "read_repo_file").fn({"path": "src/rumil/prompts/self_improve.md"})
    assert "rumil" in out.lower()


@pytest.mark.parametrize("bad", ["/etc/passwd", "../../etc/passwd", ""])
async def test_read_repo_file_blocks_path_escape(
    tmp_db,
    question_page,
    bad,
):
    tools = _build_tools(question_page.id, [(question_page, 0)], [], tmp_db)
    out = await _get_tool(tools, "read_repo_file").fn({"path": bad})
    lowered = out.lower()
    assert "outside" in lowered or "invalid" in lowered


async def test_read_repo_file_missing_file_returns_error(
    tmp_db,
    question_page,
):
    tools = _build_tools(question_page.id, [(question_page, 0)], [], tmp_db)
    out = await _get_tool(tools, "read_repo_file").fn({"path": "zzz_does_not_exist_xyz.md"})
    assert "does not exist" in out.lower()


async def test_list_repo_dir_shows_visible_entries(tmp_db, question_page):
    tools = _build_tools(question_page.id, [(question_page, 0)], [], tmp_db)
    out = await _get_tool(tools, "list_repo_dir").fn({"path": "."})
    assert "src/" in out
    assert "main.py" in out


async def test_list_repo_dir_filters_ignored_and_hidden(
    tmp_db,
    question_page,
):
    tools = _build_tools(question_page.id, [(question_page, 0)], [], tmp_db)
    out = await _get_tool(tools, "list_repo_dir").fn({"path": "."})
    assert ".git" not in out
    assert "pages/" not in out
    assert "__pycache__" not in out


async def test_list_repo_dir_rejects_escape(tmp_db, question_page):
    tools = _build_tools(question_page.id, [(question_page, 0)], [], tmp_db)
    out = await _get_tool(tools, "list_repo_dir").fn({"path": "/etc"})
    assert "outside" in out.lower() or "invalid" in out.lower()


def test_save_self_improvement_writes_file(monkeypatch, tmp_path):
    monkeypatch.setattr(si, "OUTPUT_DIR", tmp_path / "out")
    path = save_self_improvement(
        "# Analysis\n\nHello world",
        "Is the sky blue?",
    )
    assert path.exists()
    assert path.read_text() == "# Analysis\n\nHello world"
    assert "is-the-sky-blue" in path.name
    assert path.suffix == ".md"


def test_save_self_improvement_sanitises_headline(monkeypatch, tmp_path):
    monkeypatch.setattr(si, "OUTPUT_DIR", tmp_path / "out")
    path = save_self_improvement(
        "x",
        "Weird/characters*and?punctuation: here!",
    )
    for bad in "/*?:!":
        assert bad not in path.name


async def test_run_self_improvement_rejects_missing_question(tmp_db):
    with pytest.raises(ValueError, match="not found"):
        await run_self_improvement(
            "00000000-0000-0000-0000-000000000000",
            tmp_db,
        )


async def test_run_self_improvement_rejects_non_question_page(tmp_db):
    claim = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="not a question",
        headline="A claim",
    )
    await tmp_db.save_page(claim)
    with pytest.raises(ValueError, match="not a question"):
        await run_self_improvement(claim.id, tmp_db)


async def test_run_self_improvement_omits_steering_block_when_no_instructions(
    monkeypatch, tmp_db, question_page
):
    captured: dict[str, str] = {}

    async def fake_run_agent(system_prompt, user_message, tools):
        captured["user_message"] = user_message
        return ""

    monkeypatch.setattr(si, "_run_agent", fake_run_agent)
    await run_self_improvement(question_page.id, tmp_db)
    assert "Steering from the user" not in captured["user_message"]


async def test_run_self_improvement_interpolates_instructions(monkeypatch, tmp_db, question_page):
    captured: dict[str, str] = {}

    async def fake_run_agent(system_prompt, user_message, tools):
        captured["user_message"] = user_message
        return ""

    monkeypatch.setattr(si, "_run_agent", fake_run_agent)
    await run_self_improvement(
        question_page.id,
        tmp_db,
        instructions="focus on prioritization quality",
    )
    msg = captured["user_message"]
    assert "Steering from the user" in msg
    assert "focus on prioritization quality" in msg


async def test_run_self_improvement_treats_blank_instructions_as_none(
    monkeypatch, tmp_db, question_page
):
    captured: dict[str, str] = {}

    async def fake_run_agent(system_prompt, user_message, tools):
        captured["user_message"] = user_message
        return ""

    monkeypatch.setattr(si, "_run_agent", fake_run_agent)
    await run_self_improvement(question_page.id, tmp_db, instructions="   \n  ")
    assert "Steering from the user" not in captured["user_message"]


REPO_ROOT = Path(__file__).resolve().parent.parent


def test_cli_self_improve_flag_in_help():
    result = subprocess.run(
        ["uv", "run", "python", "main.py", "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0
    assert "--self-improve" in result.stdout
    assert "[QUESTION_ID]" in result.stdout, (
        "--self-improve must be rendered with an optional argument "
        "(nargs='?') so it can be combined with a new question for "
        "auto-analysis"
    )


def test_cli_self_improve_dispatches_and_handles_missing_id():
    """End-to-end: argparse accepts the flag and cmd_self_improve runs
    (reports 'not found' for an unknown question id)."""
    scratch_workspace = f"self-improve-cli-{uuid.uuid4().hex[:8]}"
    result = subprocess.run(
        [
            "uv",
            "run",
            "python",
            "main.py",
            "--self-improve",
            "nonexistent-id",
            "--workspace",
            scratch_workspace,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    combined = result.stdout + result.stderr
    assert "unrecognized" not in combined.lower()
    assert "not found" in combined.lower()


def test_cli_self_improve_bare_flag_does_not_consume_next_flag():
    """--self-improve without an id should set the sentinel and let the
    next flag dispatch normally (nargs='?' behaviour). We combine it with
    --list, which returns early, to confirm argparse didn't consume
    '--list' as the question id."""
    scratch_workspace = f"self-improve-bare-{uuid.uuid4().hex[:8]}"
    result = subprocess.run(
        [
            "uv",
            "run",
            "python",
            "main.py",
            "--self-improve",
            "--list",
            "--workspace",
            scratch_workspace,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    combined = result.stdout + result.stderr
    assert "unrecognized" not in combined.lower()
    assert "expected one argument" not in combined.lower()
    assert "Self-improvement analysis for" not in combined, (
        "--list should short-circuit before self-improve runs"
    )
    assert (
        f"No questions in workspace '{scratch_workspace}'" in combined
        or scratch_workspace in combined
    )
