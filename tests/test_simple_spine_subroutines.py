"""Tests for SimpleSpine subroutine kinds — artifact channel + guards.

Covers:
- ``CallTypeSubroutine`` / ``NestedOrchSubroutine`` __post_init__
  raise loudly when ``consumes`` is non-empty (out of MVP scope).
- ``FreeformAgentSubroutine`` and ``SampleNSubroutine`` prepend the
  rendered artifact block to the user message at spawn time, and
  default ``produces={"": <body>}`` so the orchestrator can fold the
  output into the run's ArtifactStore.

Mocks the LLM at the API boundary (``thin_agent_loop`` for
FreeformAgent, ``call_anthropic_api`` for SampleN) so tests stay fast.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from rumil.calls.find_considerations import FindConsiderationsCall
from rumil.models import CallType
from rumil.orchestrators.simple_spine.agent_loop import ThinLoopResult
from rumil.orchestrators.simple_spine.artifacts import ArtifactStore
from rumil.orchestrators.simple_spine.subroutines import (
    CallTypeSubroutine,
    FreeformAgentSubroutine,
    NestedOrchSubroutine,
    SampleNSubroutine,
    SpawnCtx,
)


def _spawn_ctx(
    *,
    db,
    artifacts: ArtifactStore | None = None,
    include: tuple[str, ...] = (),
    operating_assumptions: str = "",
):
    """Minimal SpawnCtx for subroutine.run() — clock + broadcaster stubbed."""
    clock = MagicMock()
    clock.tokens_remaining = 1_000_000
    clock.tokens_exhausted = False
    clock.record_tokens = MagicMock()

    def _carve_child(cap):
        child = MagicMock()
        child.tokens_remaining = cap
        child.tokens_exhausted = False
        child.record_tokens = MagicMock()
        return child

    clock.carve_child.side_effect = _carve_child

    return SpawnCtx(
        db=db,
        budget_clock=clock,
        broadcaster=None,
        parent_call_id="call-1",
        question_id="q-1",
        spawn_id="spawn-1",
        operating_assumptions=operating_assumptions,
        artifacts=artifacts,
        include_artifacts=include,
    )


def test_call_type_rejects_consumes_in_post_init():
    with pytest.raises(ValueError, match="consumes is not yet supported"):
        CallTypeSubroutine(
            name="x",
            description="d",
            call_type=CallType.FIND_CONSIDERATIONS,
            runner_cls=FindConsiderationsCall,
            consumes=("pair_text",),
        )


def test_call_type_accepts_empty_consumes():
    sub = CallTypeSubroutine(
        name="x",
        description="d",
        call_type=CallType.FIND_CONSIDERATIONS,
        runner_cls=FindConsiderationsCall,
    )
    assert sub.consumes == ()


def test_call_type_skips_include_artifacts_in_schema():
    """call_type kinds don't render through the spine's spawn user
    prompt path, so they shouldn't expose include_artifacts on their
    spawn tool — silently advertising it would mislead mainline.
    """
    sub = CallTypeSubroutine(
        name="x",
        description="d",
        call_type=CallType.FIND_CONSIDERATIONS,
        runner_cls=FindConsiderationsCall,
    )
    schema = sub.spawn_tool_schema()
    assert "include_artifacts" not in schema["properties"]


def test_nested_orch_rejects_consumes_in_post_init():
    factory = AsyncMock(return_value="result text")
    with pytest.raises(ValueError, match="consumes is not yet supported"):
        NestedOrchSubroutine(
            name="x",
            description="d",
            orch_kind="simple_spine",
            factory=factory,
            base_token_cap=10_000,
            consumes=("pair_text",),
        )


def test_nested_orch_skips_include_artifacts_in_schema():
    factory = AsyncMock(return_value="result text")
    sub = NestedOrchSubroutine(
        name="x",
        description="d",
        orch_kind="simple_spine",
        factory=factory,
        base_token_cap=10_000,
    )
    schema = sub.spawn_tool_schema()
    assert "include_artifacts" not in schema["properties"]


def test_freeform_and_sample_n_expose_include_artifacts_in_schema():
    f = FreeformAgentSubroutine(
        name="freeform",
        description="d",
        sys_prompt="sys",
        user_prompt_template="user {intent} {additional_context}",
        model="claude-haiku-4-5",
    )
    s = SampleNSubroutine(
        name="sample",
        description="d",
        sys_prompt="sys",
        user_prompt_template="user {intent} {additional_context}",
        model="claude-haiku-4-5",
    )
    assert "include_artifacts" in f.spawn_tool_schema()["properties"]
    assert "include_artifacts" in s.spawn_tool_schema()["properties"]


@pytest.mark.asyncio
async def test_freeform_agent_prepends_artifact_block_and_produces_final_text(mocker):
    """When the spawn ctx carries an ArtifactStore + consumes, the
    rendered user_message MUST start with the XML-fenced artifact block
    and produces MUST default to {"": result.final_text} so the
    orchestrator can fold it into the store.
    """
    captured = {}

    async def _fake_thin_loop(**kwargs):
        captured["system_prompt"] = kwargs["system_prompt"]
        captured["messages"] = kwargs["messages"]
        return ThinLoopResult(
            final_text="THE FINAL TEXT",
            messages=kwargs["messages"],
            tool_calls=[],
            rounds=1,
            stopped_because="no_tool_calls",
        )

    mocker.patch(
        "rumil.orchestrators.simple_spine.subroutines.freeform_agent.thin_agent_loop",
        new=_fake_thin_loop,
    )

    store = ArtifactStore(seed={"pair_text": "## Body\nBODY", "rubric": "rubric body"})
    sub = FreeformAgentSubroutine(
        name="pair_notes",
        description="d",
        sys_prompt="SYS",
        user_prompt_template="## Intent\n{intent}\n",
        model="claude-haiku-4-5",
        consumes=("pair_text", "rubric"),
    )
    ctx = _spawn_ctx(db=MagicMock(), artifacts=store)

    result = await sub.run(ctx, {"intent": "focus on argument"})

    user_message = captured["messages"][0]["content"]
    assert user_message.startswith("## Artifacts")
    assert 'key="pair_text"' in user_message
    assert 'key="rubric"' in user_message
    # Template body comes after the artifact block.
    assert user_message.index("## Artifacts") < user_message.index("## Intent")

    assert result.produces == {"": "THE FINAL TEXT"}
    # text_summary still carries the metadata header for the model to read.
    assert "THE FINAL TEXT" in result.text_summary


@pytest.mark.asyncio
async def test_freeform_agent_no_consumes_no_block_no_produces_change(mocker):
    """When the spawn declares no consumes / include, no artifact block
    is prepended (and the user prompt is exactly the rendered template).
    """
    captured = {}

    async def _fake_thin_loop(**kwargs):
        captured["messages"] = kwargs["messages"]
        return ThinLoopResult(
            final_text="OUT",
            messages=kwargs["messages"],
            tool_calls=[],
            rounds=1,
            stopped_because="no_tool_calls",
        )

    mocker.patch(
        "rumil.orchestrators.simple_spine.subroutines.freeform_agent.thin_agent_loop",
        new=_fake_thin_loop,
    )

    sub = FreeformAgentSubroutine(
        name="freeform",
        description="d",
        sys_prompt="SYS",
        user_prompt_template="user prompt: {intent}",
        model="claude-haiku-4-5",
    )
    ctx = _spawn_ctx(db=MagicMock())  # no artifacts
    await sub.run(ctx, {"intent": "anything"})
    user_message = captured["messages"][0]["content"]
    assert "## Artifacts" not in user_message
    assert user_message == "user prompt: anything"


@pytest.mark.asyncio
async def test_sample_n_prepends_artifact_block_and_produces_joined_body(mocker):
    """SampleN renders the artifact block once into the shared user
    message; produces["" ] is the joined sample bodies (no metadata
    header) so the orchestrator can splice it into a downstream consume.
    """
    captured = {"messages": []}

    # SampleN constructs the anthropic client eagerly via
    # ``anthropic.AsyncAnthropic(api_key=get_settings().require_anthropic_key())``;
    # the conftest autouse fixture patches require_anthropic_key to
    # raise. Re-patch here to return a fake key — the call_anthropic_api
    # mock below ensures no network traffic.
    from rumil.settings import Settings

    mocker.patch.object(Settings, "require_anthropic_key", return_value="sk-fake")

    from anthropic.types import TextBlock

    async def _fake_call_api(client, model, sys, msgs, *args, **kwargs):
        captured["messages"].append(msgs)
        msg = MagicMock()
        msg.usage = MagicMock(input_tokens=10, output_tokens=20)
        # Real TextBlock instance — sample_n's content loop uses
        # ``isinstance(block, TextBlock)`` to extract text.
        msg.content = [TextBlock(text="SAMPLE TEXT", type="text")]
        api_resp = MagicMock()
        api_resp.message = msg
        return api_resp

    mocker.patch(
        "rumil.orchestrators.simple_spine.subroutines.sample_n.call_anthropic_api",
        new=_fake_call_api,
    )

    store = ArtifactStore(seed={"pair_text": "P", "rubric": "R"})
    sub = SampleNSubroutine(
        name="steelman",
        description="d",
        sys_prompt="SYS",
        user_prompt_template="## Intent\n{intent}\n",
        model="claude-haiku-4-5",
        n=2,
        max_tokens=2048,
        consumes=("pair_text", "rubric"),
    )
    ctx = _spawn_ctx(db=MagicMock(), artifacts=store)
    result = await sub.run(ctx, {"intent": "A"})

    # All N samples saw the same shared user message, prefixed with the artifact block.
    for msgs in captured["messages"]:
        body = msgs[0]["content"]
        assert body.startswith("## Artifacts")
        assert 'key="pair_text"' in body

    # produces[""] is just the sample bodies joined — no "## Sample N — ..." header.
    assert result.produces[""].count("SAMPLE TEXT") == 2
    assert "(intent: A)" not in result.produces[""]


@pytest.mark.asyncio
async def test_sample_n_no_completions_no_produces_entry(mocker):
    """If the affordability check skips every sample (token budget too
    tight), produces is empty so the orchestrator doesn't add a no-op
    artifact key.
    """
    from rumil.settings import Settings

    mocker.patch.object(Settings, "require_anthropic_key", return_value="sk-fake")
    # Force the spawn clock to have ~0 remaining so the affordability
    # check skips all N. _spawn_ctx's carve_child takes a cap, but the
    # SampleN run uses spawn_clock.tokens_remaining // per_sample_worst,
    # so passing a tiny cap forces zero affordable.
    mocker.patch(
        "rumil.orchestrators.simple_spine.subroutines.sample_n.call_anthropic_api",
        new=AsyncMock(),  # never awaited because affordability skips all
    )

    sub = SampleNSubroutine(
        name="sample",
        description="d",
        sys_prompt="SYS",
        user_prompt_template="user {intent}",
        model="claude-haiku-4-5",
        n=3,
        max_tokens=4096,
        base_token_cap=1,  # forces carve_child to clamp to 1 → nothing affordable
    )
    ctx = _spawn_ctx(db=MagicMock())
    result = await sub.run(ctx, {"intent": "x"})
    assert result.produces == {}
    assert result.extra["samples_run"] == 0
    assert result.extra["samples_skipped"] == 3
