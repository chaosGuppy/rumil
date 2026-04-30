"""Tests for prompt-building helpers used across the call architecture.

Covers:
- ``build_system_prompt`` task substitution and per-call inclusion flag.
- ``build_user_message`` per-call inclusion via ``call_type``.
- ``embed_task_for_page`` page lookup, label override, and missing-page fallback.
"""

import pytest

from rumil.calls.common import embed_task_for_page
from rumil.llm import build_system_prompt, build_user_message
from rumil.models import Page, PageLayer, PageType, Workspace
from rumil.prompts import PROMPTS_DIR


PER_CALL_PROBE = "find_considerations"


def _load_per_call_text(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def test_build_system_prompt_substitutes_task():
    prompt = build_system_prompt(
        PER_CALL_PROBE,
        task="investigating-a-very-distinctive-marker",
        include_per_call=False,
    )
    assert "investigating-a-very-distinctive-marker" in prompt
    assert "{{TASK}}" not in prompt


def test_build_system_prompt_without_task_replaces_placeholder():
    prompt = build_system_prompt(PER_CALL_PROBE, include_per_call=False)
    assert "{{TASK}}" not in prompt


def test_build_system_prompt_include_per_call_toggle():
    per_call = _load_per_call_text(PER_CALL_PROBE).strip()
    assert per_call, "per-call prompt file is unexpectedly empty"

    with_per_call = build_system_prompt(PER_CALL_PROBE, include_per_call=True)
    without_per_call = build_system_prompt(PER_CALL_PROBE, include_per_call=False)

    assert per_call in with_per_call
    assert per_call not in without_per_call
    assert len(with_per_call) > len(without_per_call)


def test_build_user_message_call_type_toggle():
    per_call = _load_per_call_text(PER_CALL_PROBE).strip()
    msg_with = build_user_message("ctx", "go do the thing", call_type=PER_CALL_PROBE)
    msg_without = build_user_message("ctx", "go do the thing", call_type=None)

    assert per_call in msg_with
    assert per_call not in msg_without
    assert "go do the thing" in msg_with
    assert "go do the thing" in msg_without


def test_build_user_message_omits_empty_context():
    msg = build_user_message("", "the task", call_type=None)
    assert msg == "the task"


@pytest.fixture
async def claim_page(tmp_db):
    page = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Routine cognitive labour is automatable within a decade.",
        headline="Routine cognitive labour is automatable within a decade.",
    )
    await tmp_db.save_page(page)
    return page


async def test_embed_task_for_page_question(tmp_db, question_page):
    framing = "fan-out scouting prioritization."
    result = await embed_task_for_page(tmp_db, question_page.id, framing)

    assert question_page.headline in result
    assert "the question being investigated" in result
    assert result.endswith(framing)


async def test_embed_task_for_page_claim_label(tmp_db, claim_page):
    framing = "main-phase prioritization for claim investigation."
    result = await embed_task_for_page(tmp_db, claim_page.id, framing, label="claim")

    assert claim_page.headline in result
    assert "the claim being investigated" in result
    assert "the question being investigated" not in result


async def test_embed_task_for_page_missing_page_falls_back(tmp_db):
    framing = "do the thing."
    result = await embed_task_for_page(tmp_db, "00000000-0000-0000-0000-000000000000", framing)
    assert result == framing
