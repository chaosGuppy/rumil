"""Tests for the find_considerations prompt variant wiring.

Covers:
- build_system_prompt loads the correct .md file based on find_considerations_variant
- MultiRoundLoop (the workspace updater used by FindConsiderationsCall) passes
  source-first instructions to the LLM when the variant is active
- Other call types are unaffected by the setting
"""

import pytest

from rumil.calls.page_creators import MultiRoundLoop
from rumil.calls.stages import CallInfra, ContextResult
from rumil.llm import AgentResult, build_system_prompt
from rumil.models import CallType, FindConsiderationsMode, MoveType
from rumil.moves.base import MoveState
from rumil.settings import override_settings
from rumil.tracing.tracer import CallTrace


def test_build_system_prompt_default_variant_uses_default_file():
    with override_settings(rumil_test_mode="1", find_considerations_variant="default"):
        prompt = build_system_prompt("find_considerations")

    assert "Find Considerations Call Instructions" in prompt
    assert "Source-First Variant" not in prompt
    assert "Source-First Policy" not in prompt


def test_build_system_prompt_source_first_variant_uses_source_first_file():
    with override_settings(rumil_test_mode="1", find_considerations_variant="source_first"):
        prompt = build_system_prompt("find_considerations")

    assert "Source-First Variant" in prompt
    assert "Source-First Policy" in prompt
    assert "web_research" in prompt
    assert "ingest" in prompt


def test_build_system_prompt_invalid_variant_raises():
    with override_settings(rumil_test_mode="1", find_considerations_variant="nonsense"):
        with pytest.raises(ValueError, match="find_considerations_variant"):
            build_system_prompt("find_considerations")


def test_build_system_prompt_other_call_types_unaffected_by_variant():
    """Setting find_considerations_variant shouldn't change other call types' prompts."""
    with override_settings(rumil_test_mode="1", find_considerations_variant="source_first"):
        assess_prompt = build_system_prompt("assess")

    assert "Source-First Policy" not in assess_prompt


async def test_multi_round_loop_sends_source_first_prompt_to_llm(
    tmp_db, question_page, scout_call, mocker
):
    """MultiRoundLoop (used by FindConsiderationsCall) must pass the source-first
    system prompt to run_agent_loop when the variant is active."""
    captured_system_prompts: list[str] = []

    async def fake_loop(system_prompt, user_message=None, tools=None, **kwargs):
        captured_system_prompts.append(system_prompt)
        return AgentResult(text="(stub)")

    mocker.patch(
        "rumil.calls.page_creators.run_agent_loop",
        side_effect=fake_loop,
    )

    trace = CallTrace(scout_call.id, tmp_db)
    state = MoveState(scout_call, tmp_db)
    infra = CallInfra(
        question_id=question_page.id,
        call=scout_call,
        db=tmp_db,
        trace=trace,
        state=state,
    )
    context = ContextResult(
        context_text="(stub context)",
        working_page_ids=[],
        preloaded_ids=[],
        phase1_ids=[],
    )

    updater = MultiRoundLoop(
        max_rounds=1,
        fruit_threshold=0,
        mode=FindConsiderationsMode.ABSTRACT,
        available_moves=[MoveType.CREATE_CLAIM],
        call_type=CallType.FIND_CONSIDERATIONS,
    )

    with override_settings(rumil_test_mode="1", find_considerations_variant="source_first"):
        await updater.update_workspace(infra, context)

    assert captured_system_prompts, "run_agent_loop should have been called at least once"
    first_prompt = captured_system_prompts[0]
    assert "Source-First Policy" in first_prompt
    assert "web_research" in first_prompt


async def test_multi_round_loop_default_variant_does_not_send_source_first(
    tmp_db, question_page, scout_call, mocker
):
    """Sanity check: with the default variant, source-first markers are absent."""
    captured_system_prompts: list[str] = []

    async def fake_loop(system_prompt, user_message=None, tools=None, **kwargs):
        captured_system_prompts.append(system_prompt)
        return AgentResult(text="(stub)")

    mocker.patch(
        "rumil.calls.page_creators.run_agent_loop",
        side_effect=fake_loop,
    )

    trace = CallTrace(scout_call.id, tmp_db)
    state = MoveState(scout_call, tmp_db)
    infra = CallInfra(
        question_id=question_page.id,
        call=scout_call,
        db=tmp_db,
        trace=trace,
        state=state,
    )
    context = ContextResult(
        context_text="(stub context)",
        working_page_ids=[],
        preloaded_ids=[],
        phase1_ids=[],
    )

    updater = MultiRoundLoop(
        max_rounds=1,
        fruit_threshold=0,
        mode=FindConsiderationsMode.ABSTRACT,
        available_moves=[MoveType.CREATE_CLAIM],
        call_type=CallType.FIND_CONSIDERATIONS,
    )

    with override_settings(rumil_test_mode="1", find_considerations_variant="default"):
        await updater.update_workspace(infra, context)

    assert captured_system_prompts
    assert "Source-First Policy" not in captured_system_prompts[0]
