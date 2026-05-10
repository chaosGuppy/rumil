"""Integration-level tests for AxonOrchestrator with a mocked LLM.

Each test scripts the sequence of mainline / configure / inner-loop API
responses by patching ``call_anthropic_api`` at both import sites
(orchestrator + runner) and feeding scripted ``Message`` objects.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
import yaml
from anthropic.types import Message, TextBlock, ToolUseBlock, Usage

from rumil.llm import APIResponse
from rumil.models import Page, PageLayer, PageType, Workspace
from rumil.orchestrators.axon import (
    AxonOrchestrator,
    DirectToolCtx,
    OrchInputs,
    direct_tool_ctx_scope,
    load_axon_config,
)
from rumil.orchestrators.axon.tools import (
    CONFIGURE_TOOL_NAME,
    DELEGATE_TOOL_NAME,
    FINALIZE_TOOL_NAME,
)
from rumil.orchestrators.axon.trace_events import (
    AxonInnerLoopStartedEvent,
)


def _msg(content: list, *, model: str = "claude-haiku-4-5") -> Message:
    return Message(
        id=f"msg_{uuid.uuid4().hex[:8]}",
        type="message",
        role="assistant",
        content=content,
        model=model,
        stop_reason="end_turn",
        usage=Usage(input_tokens=10, output_tokens=5),
    )


def _api_response(content: list) -> APIResponse:
    return APIResponse(message=_msg(content), duration_ms=1)


def _text_only_response(text: str) -> APIResponse:
    return _api_response([TextBlock(type="text", text=text)])


def _tool_use_block(name: str, inp: dict, *, tool_id: str | None = None) -> ToolUseBlock:
    return ToolUseBlock(
        type="tool_use",
        id=tool_id or f"toolu_{uuid.uuid4().hex[:8]}",
        name=name,
        input=inp,
    )


def _tool_use_response(blocks: Sequence[ToolUseBlock], *, text: str = "") -> APIResponse:
    content: list = []
    if text:
        content.append(TextBlock(type="text", text=text))
    content.extend(blocks)
    return _api_response(content)


def _delegate_block(
    *,
    intent: str,
    inherit_context: bool,
    budget_usd: float,
    n: int = 1,
    tool_id: str | None = None,
) -> ToolUseBlock:
    return _tool_use_block(
        DELEGATE_TOOL_NAME,
        {
            "intent": intent,
            "inherit_context": inherit_context,
            "budget_usd": budget_usd,
            "n": n,
        },
        tool_id=tool_id,
    )


def _configure_block(
    *,
    system_prompt: dict | None = None,
    tools: list[str] | None = None,
    finalize_schema: dict | None = None,
    max_rounds: int = 3,
    side_effects: list[str] | None = None,
    artifact_key: str | None = None,
    rationale: str = "test rationale",
    extra_context: str | None = None,
) -> ToolUseBlock:
    payload: dict[str, Any] = {
        "max_rounds": max_rounds,
        "rationale": rationale,
        "finalize_schema": finalize_schema or {"ref": "freeform_text"},
        "side_effects": side_effects or [],
    }
    if system_prompt is not None:
        payload["system_prompt"] = system_prompt
    if tools is not None:
        payload["tools"] = tools
    if artifact_key is not None:
        payload["artifact_key"] = artifact_key
    if extra_context is not None:
        payload["extra_context"] = extra_context
    return _tool_use_block(CONFIGURE_TOOL_NAME, payload)


def _finalize_block(args: dict, *, tool_id: str | None = None) -> ToolUseBlock:
    return _tool_use_block(FINALIZE_TOOL_NAME, args, tool_id=tool_id)


@pytest_asyncio.fixture
async def axon_config(tmp_path: Path):
    sys_prompt = tmp_path / "sys.md"
    sys_prompt.write_text("you are the axon spine")
    web_sys = tmp_path / "web.md"
    web_sys.write_text("isolation system prompt")
    config_yaml = tmp_path / "research.yaml"
    config_yaml.write_text(
        yaml.safe_dump(
            {
                "name": "research-test",
                "main_model": "claude-haiku-4-5",
                "main_system_prompt_path": "sys.md",
                "hard_max_rounds": 6,
                "max_seed_pages": 2,
                "direct_tools": ["load_page"],
                "system_prompt_registry": {"web_research": "web.md"},
                "finalize_schema_registry": {
                    "freeform_text": {
                        "type": "object",
                        "properties": {
                            "answer": {"type": "string"},
                        },
                        "required": ["answer"],
                        "additionalProperties": False,
                    }
                },
            }
        )
    )
    return load_axon_config(config_yaml)


class _ScriptedAPI:
    """Records calls and returns scripted APIResponse objects in order.

    Splits responses by ``phase`` so the orchestrator's mainline /
    configure / inner-loop calls can be scripted independently.
    """

    def __init__(self) -> None:
        self.mainline: list[APIResponse] = []
        self.configure: list[APIResponse] = []
        self.inner: list[APIResponse] = []
        self.calls: list[dict] = []

    async def __call__(
        self,
        client,
        model,
        system_prompt,
        messages,
        tools=None,
        warnings=None,
        metadata=None,
        db=None,
        cache=False,
        **_,
    ):
        phase = (metadata.phase if metadata is not None else "") or ""
        record = {
            "phase": phase,
            "system_prompt": system_prompt,
            "messages": list(messages),
            "tools": list(tools or []),
            "model": model,
        }
        self.calls.append(record)
        if phase.startswith("configure"):
            queue = self.configure
        elif phase.startswith("inner"):
            queue = self.inner
        else:
            queue = self.mainline
        if not queue:
            raise AssertionError(
                f"_ScriptedAPI: no scripted response left for phase={phase!r}; "
                f"recorded calls so far: {len(self.calls)}"
            )
        return queue.pop(0)


@pytest.fixture
def scripted_api(mocker):
    api = _ScriptedAPI()
    mocker.patch("rumil.settings.Settings.require_anthropic_key", return_value="sk-fake")
    mocker.patch("rumil.orchestrators.axon.orchestrator.call_anthropic_api", new=api)
    mocker.patch("rumil.orchestrators.axon.runner.call_anthropic_api", new=api)
    mocker.patch(
        "rumil.orchestrators.axon.orchestrator.anthropic.AsyncAnthropic",
        return_value=mocker.MagicMock(),
    )
    mocker.patch(
        "rumil.orchestrators.axon.runner.anthropic.AsyncAnthropic",
        return_value=mocker.MagicMock(),
    )
    return api


def _orch_inputs(question: str = "What is X?", *, budget: float = 5.0, **kw) -> OrchInputs:
    return OrchInputs(question=question, budget_usd=budget, **kw)


@pytest.mark.asyncio
async def test_single_delegate_continuation_then_finalize(tmp_db, axon_config, scripted_api):
    orch = AxonOrchestrator(db=tmp_db, config=axon_config)
    delegate_id = "toolu_dlg1"
    scripted_api.mainline.append(
        _tool_use_response(
            [
                _delegate_block(
                    intent="distill recent thinking",
                    inherit_context=True,
                    budget_usd=1.0,
                    tool_id=delegate_id,
                )
            ],
            text="Spawning a delegate.",
        )
    )
    scripted_api.mainline.append(
        _tool_use_response(
            [_finalize_block({"answer": "final mainline answer"})],
            text="Done.",
        )
    )
    scripted_api.configure.append(
        _tool_use_response(
            [
                _configure_block(
                    system_prompt=None,
                    tools=None,
                    finalize_schema={"ref": "freeform_text"},
                    rationale="continuation",
                )
            ]
        )
    )
    scripted_api.inner.append(
        _tool_use_response(
            [_finalize_block({"answer": "delegate result text"})],
        )
    )

    result = await orch.run(_orch_inputs())

    assert result.answer_text == "final mainline answer"
    assert result.last_status == "completed"

    inner_call = next(c for c in scripted_api.calls if c["phase"].startswith("inner"))
    inner_tool_names = {t["name"] for t in inner_call["tools"]}
    assert {
        DELEGATE_TOOL_NAME,
        CONFIGURE_TOOL_NAME,
        FINALIZE_TOOL_NAME,
        "load_page",
    } <= inner_tool_names

    configure_call = next(c for c in scripted_api.calls if c["phase"].startswith("configure"))
    last_user_block = configure_call["messages"][-1]
    assert last_user_block["role"] == "user"
    inner_blocks = last_user_block["content"]
    placeholders = [b for b in inner_blocks if b.get("type") == "tool_result"]
    assert len(placeholders) == 1
    assert placeholders[0]["tool_use_id"] == delegate_id

    second_mainline = scripted_api.calls[-1]
    assert second_mainline["phase"] == "mainline"
    last_user = second_mainline["messages"][-1]
    tool_results = [b for b in last_user["content"] if b.get("type") == "tool_result"]
    assert len(tool_results) == 1
    assert tool_results[0]["tool_use_id"] == delegate_id
    assert "delegate result text" in tool_results[0]["content"]


@pytest.mark.asyncio
async def test_single_delegate_isolation_then_finalize(tmp_db, axon_config, scripted_api):
    orch = AxonOrchestrator(db=tmp_db, config=axon_config)
    scripted_api.mainline.append(
        _tool_use_response(
            [
                _delegate_block(
                    intent="critic of stance X",
                    inherit_context=False,
                    budget_usd=1.0,
                )
            ]
        )
    )
    scripted_api.mainline.append(_tool_use_response([_finalize_block({"answer": "all done"})]))
    scripted_api.configure.append(
        _tool_use_response(
            [
                _configure_block(
                    system_prompt={"ref": "web_research"},
                    tools=["load_page"],
                    finalize_schema={"ref": "freeform_text"},
                    rationale="isolation",
                )
            ]
        )
    )
    scripted_api.inner.append(_tool_use_response([_finalize_block({"answer": "critic says no"})]))

    result = await orch.run(_orch_inputs())
    assert result.answer_text == "all done"

    spine_first_user = scripted_api.calls[0]["messages"][0]["content"]
    inner_call = next(c for c in scripted_api.calls if c["phase"].startswith("inner"))
    inner_first_user = inner_call["messages"][0]["content"]
    assert inner_first_user != spine_first_user

    assert inner_call["system_prompt"].startswith("isolation system prompt")
    inner_tool_names = {t["name"] for t in inner_call["tools"]}
    assert inner_tool_names == {FINALIZE_TOOL_NAME, "load_page"}

    if isinstance(inner_first_user, str):
        inner_text = inner_first_user
    else:
        inner_text = "".join(b.get("text", "") for b in inner_first_user if isinstance(b, dict))
    assert "critic of stance X" in inner_text


@pytest.mark.asyncio
async def test_coupling_rule_corrective_retry(tmp_db, axon_config, scripted_api):
    orch = AxonOrchestrator(db=tmp_db, config=axon_config)
    scripted_api.mainline.append(
        _tool_use_response(
            [
                _delegate_block(
                    intent="continue thread",
                    inherit_context=True,
                    budget_usd=0.5,
                )
            ]
        )
    )
    scripted_api.mainline.append(_tool_use_response([_finalize_block({"answer": "wrap"})]))
    scripted_api.configure.append(
        _tool_use_response(
            [
                _configure_block(
                    system_prompt={"inline": "I should not be set"},
                    tools=None,
                    rationale="bad attempt",
                )
            ]
        )
    )
    scripted_api.configure.append(
        _tool_use_response(
            [
                _configure_block(
                    system_prompt=None,
                    tools=None,
                    rationale="corrected",
                )
            ]
        )
    )
    scripted_api.inner.append(_tool_use_response([_finalize_block({"answer": "ok"})]))

    result = await orch.run(_orch_inputs())
    assert result.last_status == "completed"
    assert result.answer_text == "wrap"
    configure_calls = [c for c in scripted_api.calls if c["phase"].startswith("configure")]
    assert len(configure_calls) == 2
    retry_user = configure_calls[1]["messages"][-1]["content"]
    retry_text = "\n".join(b.get("text", "") for b in retry_user if isinstance(b, dict))
    assert "[retry]" in retry_text


@pytest.mark.asyncio
async def test_n_sample_runs_n_inner_loops(tmp_db, axon_config, scripted_api, mocker):
    orch = AxonOrchestrator(db=tmp_db, config=axon_config)
    scripted_api.mainline.append(
        _tool_use_response(
            [
                _delegate_block(
                    intent="run 3 samples",
                    inherit_context=True,
                    budget_usd=2.0,
                    n=3,
                )
            ]
        )
    )
    scripted_api.mainline.append(_tool_use_response([_finalize_block({"answer": "consolidated"})]))
    scripted_api.configure.append(
        _tool_use_response(
            [
                _configure_block(
                    system_prompt=None,
                    tools=None,
                    rationale="3-way",
                )
            ]
        )
    )
    for i in range(3):
        scripted_api.inner.append(_tool_use_response([_finalize_block({"answer": f"sample {i}"})]))

    inner_started_events: list[AxonInnerLoopStartedEvent] = []
    real_record = None
    from rumil.tracing.tracer import CallTrace

    real_record = CallTrace.record

    async def _capture_record(self, event):
        if isinstance(event, AxonInnerLoopStartedEvent):
            inner_started_events.append(event)
        return await real_record(self, event)

    mocker.patch.object(CallTrace, "record", _capture_record)

    result = await orch.run(_orch_inputs(budget=10.0))
    assert result.last_status == "completed"
    inner_calls = [c for c in scripted_api.calls if c["phase"].startswith("inner")]
    assert len(inner_calls) == 3
    configure_calls = [c for c in scripted_api.calls if c["phase"].startswith("configure")]
    assert len(configure_calls) == 1
    assert len(inner_started_events) == 3
    assert {e.sample_idx for e in inner_started_events} == {0, 1, 2}

    final_mainline = [c for c in scripted_api.calls if c["phase"] == "mainline"][-1]
    last_user = final_mainline["messages"][-1]
    tool_results = [b for b in last_user["content"] if b.get("type") == "tool_result"]
    content = tool_results[0]["content"]
    assert "n=3 samples" in content
    assert "sample 0" in content
    assert "sample 1" in content
    assert "sample 2" in content


@pytest.mark.asyncio
async def test_artifact_write_side_effect_n_one(tmp_db, axon_config, scripted_api):
    orch = AxonOrchestrator(db=tmp_db, config=axon_config)
    captured_store: dict[str, Any] = {}
    original_dispatch = orch._dispatch_one_delegate

    async def _capture(*args, **kwargs):
        captured_store["artifacts"] = kwargs["artifacts"]
        return await original_dispatch(*args, **kwargs)

    orch._dispatch_one_delegate = _capture  # type: ignore[method-assign]

    scripted_api.mainline.append(
        _tool_use_response(
            [
                _delegate_block(
                    intent="distill",
                    inherit_context=True,
                    budget_usd=1.0,
                )
            ]
        )
    )
    scripted_api.mainline.append(_tool_use_response([_finalize_block({"answer": "complete"})]))
    scripted_api.configure.append(
        _tool_use_response(
            [
                _configure_block(
                    system_prompt=None,
                    tools=None,
                    side_effects=["write_artifact"],
                    artifact_key="dist1",
                    rationale="persist",
                )
            ]
        )
    )
    scripted_api.inner.append(
        _tool_use_response([_finalize_block({"answer": "distillation body"})])
    )

    await orch.run(_orch_inputs())
    store = captured_store["artifacts"]
    assert "dist1" in store
    art = store.get("dist1")
    assert "distillation body" in art.text


@pytest.mark.asyncio
async def test_artifact_write_side_effect_n_three(tmp_db, axon_config, scripted_api):
    orch = AxonOrchestrator(db=tmp_db, config=axon_config)
    captured_store: dict[str, Any] = {}
    original_dispatch = orch._dispatch_one_delegate

    async def _capture(*args, **kwargs):
        captured_store["artifacts"] = kwargs["artifacts"]
        return await original_dispatch(*args, **kwargs)

    orch._dispatch_one_delegate = _capture  # type: ignore[method-assign]

    scripted_api.mainline.append(
        _tool_use_response(
            [
                _delegate_block(
                    intent="3 distills",
                    inherit_context=True,
                    budget_usd=2.0,
                    n=3,
                )
            ]
        )
    )
    scripted_api.mainline.append(_tool_use_response([_finalize_block({"answer": "done"})]))
    scripted_api.configure.append(
        _tool_use_response(
            [
                _configure_block(
                    system_prompt=None,
                    tools=None,
                    side_effects=["write_artifact"],
                    artifact_key="dist1",
                    rationale="persist many",
                )
            ]
        )
    )
    for i in range(3):
        scripted_api.inner.append(_tool_use_response([_finalize_block({"answer": f"body {i}"})]))

    await orch.run(_orch_inputs(budget=10.0))
    store = captured_store["artifacts"]
    for i in range(3):
        key = f"dist1/{i}"
        assert key in store
        assert f"body {i}" in store.get(key).text


@pytest.mark.asyncio
async def test_load_page_direct_tool(tmp_db, axon_config, scripted_api):
    page = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="The page body content.",
        headline="Test page headline",
    )
    await tmp_db.save_page(page)

    orch = AxonOrchestrator(db=tmp_db, config=axon_config)
    scripted_api.mainline.append(
        _tool_use_response(
            [_tool_use_block("load_page", {"page_id": page.id}, tool_id="toolu_lp1")]
        )
    )
    scripted_api.mainline.append(_tool_use_response([_finalize_block({"answer": "have it"})]))

    ctx = DirectToolCtx(db=tmp_db, call_id="test-call")
    with direct_tool_ctx_scope(ctx):
        result = await orch.run(_orch_inputs())

    assert result.last_status == "completed"
    second_mainline = [c for c in scripted_api.calls if c["phase"] == "mainline"][1]
    last_user = second_mainline["messages"][-1]
    tool_results = [b for b in last_user["content"] if b.get("type") == "tool_result"]
    assert len(tool_results) == 1
    assert tool_results[0]["tool_use_id"] == "toolu_lp1"
    assert "Test page headline" in tool_results[0]["content"]


@pytest.mark.asyncio
async def test_seed_page_ids_rendered_in_first_user_message(tmp_db, axon_config, scripted_api):
    pages: list[Page] = []
    for i in range(3):
        page = Page(
            page_type=PageType.QUESTION,
            layer=PageLayer.SQUIDGY,
            workspace=Workspace.RESEARCH,
            content=f"q{i} content",
            headline=f"headline number {i}",
        )
        await tmp_db.save_page(page)
        pages.append(page)

    orch = AxonOrchestrator(db=tmp_db, config=axon_config)
    scripted_api.mainline.append(_tool_use_response([_finalize_block({"answer": "k"})]))

    seed_ids = [p.id for p in pages]
    await orch.run(_orch_inputs(seed_page_ids=seed_ids))

    first_user_content = scripted_api.calls[0]["messages"][0]["content"]
    rendered = "\n".join(
        b["text"] for b in first_user_content if isinstance(b, dict) and b.get("type") == "text"
    )
    assert "## Available pages" in rendered
    assert pages[0].id in rendered
    assert "headline number 0" in rendered
    assert pages[1].id in rendered
    assert "headline number 1" in rendered
    assert pages[2].id not in rendered


@pytest.mark.asyncio
async def test_finalize_payload_validation_failure(tmp_db, axon_config, scripted_api):
    orch = AxonOrchestrator(db=tmp_db, config=axon_config)
    scripted_api.mainline.append(
        _tool_use_response(
            [
                _delegate_block(
                    intent="extract",
                    inherit_context=False,
                    budget_usd=1.0,
                )
            ]
        )
    )
    scripted_api.mainline.append(_tool_use_response([_finalize_block({"answer": "recovered"})]))
    inline_schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "score": {"type": "integer"},
        },
        "required": ["title", "score"],
        "additionalProperties": False,
    }
    scripted_api.configure.append(
        _tool_use_response(
            [
                _configure_block(
                    system_prompt={"ref": "web_research"},
                    tools=[],
                    finalize_schema={"inline": inline_schema},
                    rationale="strict shape",
                )
            ]
        )
    )
    scripted_api.inner.append(_tool_use_response([_finalize_block({"title": "missing score"})]))

    result = await orch.run(_orch_inputs())
    assert result.last_status == "completed"
    assert result.answer_text == "recovered"

    second_mainline = [c for c in scripted_api.calls if c["phase"] == "mainline"][1]
    last_user = second_mainline["messages"][-1]
    tool_results = [b for b in last_user["content"] if b.get("type") == "tool_result"]
    assert len(tool_results) == 1
    tr = tool_results[0]
    assert tr.get("is_error") is True
    assert "did not finalize cleanly" in tr["content"]
    assert "score" in tr["content"]
