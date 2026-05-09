"""Tests for SimpleSpineOrchestrator artifact integration.

Pin the per-spawn lifecycle:

- Invalid ``include_artifacts`` keys raise before the spawn runs (the
  outer ``asyncio.gather`` then surfaces this as an ``is_error``
  tool_result mainline can read and retry).
- After a spawn returns, ``result.produces`` entries are folded into
  the run's ArtifactStore under namespaced keys
  (``<sub_name>/<spawn_id_short>`` for the empty sub-key,
  ``<name>/<spawn_id>/<sub_key>`` otherwise).
- The spawn's ``text_summary`` gets per-key announcement lines
  appended so mainline sees the new keys in its next turn.

Avoids the full mainline loop by exercising ``_run_spawn`` directly
with a fake SubroutineDef. Real subroutine kinds are tested in
``test_simple_spine_subroutines.py``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock

import pytest

from rumil.orchestrators.simple_spine.artifacts import ArtifactStore
from rumil.orchestrators.simple_spine.budget_clock import BudgetClock, BudgetSpec
from rumil.orchestrators.simple_spine.config import SimpleSpineConfig
from rumil.orchestrators.simple_spine.orchestrator import (
    SimpleSpineOrchestrator,
    _make_artifact_key,
)
from rumil.orchestrators.simple_spine.subroutines.base import (
    SpawnCtx,
    SubroutineBase,
    SubroutineDef,
    SubroutineResult,
)


@dataclass(frozen=True, kw_only=True)
class _FakeSub(SubroutineBase):
    """A subroutine that returns a pre-baked SubroutineResult on run.

    Skips LLM calls entirely — useful for testing orchestrator-level
    plumbing (key validation, produces folding, announcements).
    """

    canned_result: SubroutineResult = field(
        default_factory=lambda: SubroutineResult(
            text_summary="canned text", produces={"": "produced body"}
        )
    )

    def fingerprint(self) -> Mapping[str, str | int | bool | None | list[str]]:
        out = dict(super().fingerprint())
        out["kind"] = "fake"
        return out

    async def run(self, ctx: SpawnCtx, overrides: Mapping[str, object]) -> SubroutineResult:
        return self.canned_result


def _orch_with(sub: SubroutineBase) -> SimpleSpineOrchestrator:
    library: tuple[SubroutineDef, ...] = (sub,)  # pyright: ignore[reportAssignmentType]
    config = SimpleSpineConfig(
        main_model="claude-haiku-4-5",
        process_library=library,
        main_system_prompt="SYS",
    )
    db = MagicMock()
    db.run_id = "run-1"
    return SimpleSpineOrchestrator(db=db, config=config)


def _spawn_tu(name: str, **inputs):
    tu = MagicMock()
    tu.name = f"spawn_{name}"
    tu.id = "tool-use-id-1"
    tu.input = inputs
    return tu


def _trace():
    trace = MagicMock()
    trace.record = AsyncMock()
    return trace


def _make_artifact_key_helper_input_keyed_correctly():
    # Empty sub-key → "<name>/<short>".
    assert _make_artifact_key("pair_notes", "abcdef1234567890", "") == "pair_notes/abcdef12"
    # Non-empty sub-key → "<name>/<short>/<sub>".
    assert (
        _make_artifact_key("steelman", "deadbeefcafebabe", "side_a") == "steelman/deadbeef/side_a"
    )


def test_make_artifact_key_empty_sub_key():
    assert _make_artifact_key("pair_notes", "abcdef1234567890", "") == "pair_notes/abcdef12"


def test_make_artifact_key_non_empty_sub_key():
    assert (
        _make_artifact_key("steelman", "deadbeefcafebabe", "side_a") == "steelman/deadbeef/side_a"
    )


@pytest.mark.asyncio
async def test_run_spawn_invalid_include_artifact_raises():
    """An ``include_artifacts`` key that isn't in the store raises
    ValueError with the missing keys + a list of available ones; the
    outer gather turns this into an ``is_error`` tool_result so mainline
    can correct itself.
    """
    sub = _FakeSub(name="fake", description="d")
    orch = _orch_with(sub)
    store = ArtifactStore(seed={"pair_text": "P"})
    clock = BudgetClock(spec=BudgetSpec(max_tokens=1_000_000))
    tu = _spawn_tu("fake", intent="x", include_artifacts=["nonexistent"])
    with pytest.raises(ValueError, match="nonexistent"):
        await orch._run_spawn(
            tu,
            call_id="c-1",
            question_id="q-1",
            clock=clock,
            mainline_system_prompt="",
            mainline_messages=[],
            mainline_tool_uses=(),
            round_idx=0,
            trace=_trace(),
            artifact_store=store,
        )
    # Underlying spawn should NOT have been recorded as started — we
    # rejected before recording the trace event. (Pin this so a future
    # refactor that re-orders validation past trace.record breaks here.)


@pytest.mark.asyncio
async def test_run_spawn_invalid_include_artifacts_type_raises():
    sub = _FakeSub(name="fake", description="d")
    orch = _orch_with(sub)
    store = ArtifactStore()
    clock = BudgetClock(spec=BudgetSpec(max_tokens=1_000_000))
    tu = _spawn_tu("fake", intent="x", include_artifacts="not-a-list")
    with pytest.raises(ValueError, match="must be a list"):
        await orch._run_spawn(
            tu,
            call_id="c-1",
            question_id="q-1",
            clock=clock,
            mainline_system_prompt="",
            mainline_messages=[],
            mainline_tool_uses=(),
            round_idx=0,
            trace=_trace(),
            artifact_store=store,
        )


@pytest.mark.asyncio
async def test_run_spawn_folds_produces_under_namespaced_key():
    sub = _FakeSub(
        name="pair_notes",
        description="d",
        canned_result=SubroutineResult(
            text_summary="summary",
            produces={"": "output body"},
        ),
    )
    orch = _orch_with(sub)
    store = ArtifactStore()
    clock = BudgetClock(spec=BudgetSpec(max_tokens=1_000_000))
    tu = _spawn_tu("pair_notes", intent="x")
    result = await orch._run_spawn(
        tu,
        call_id="c-1",
        question_id="q-1",
        clock=clock,
        mainline_system_prompt="",
        mainline_messages=[],
        mainline_tool_uses=(),
        round_idx=0,
        trace=_trace(),
        artifact_store=store,
    )
    new_keys = result.extra["produced_artifact_keys"]
    assert len(new_keys) == 1
    full_key = new_keys[0]
    # Format: pair_notes/<8-char-spawn-id-prefix>
    assert full_key.startswith("pair_notes/")
    assert len(full_key.split("/")[1]) == 8
    art = store.get(full_key)
    assert art is not None
    assert art.text == "output body"
    assert art.produced_by == "pair_notes"


@pytest.mark.asyncio
async def test_run_spawn_folds_multi_key_produces():
    sub = _FakeSub(
        name="steelman",
        description="d",
        canned_result=SubroutineResult(
            text_summary="summary",
            produces={"side_a": "A's case", "side_b": "B's case"},
        ),
    )
    orch = _orch_with(sub)
    store = ArtifactStore()
    clock = BudgetClock(spec=BudgetSpec(max_tokens=1_000_000))
    tu = _spawn_tu("steelman", intent="A")
    result = await orch._run_spawn(
        tu,
        call_id="c-1",
        question_id="q-1",
        clock=clock,
        mainline_system_prompt="",
        mainline_messages=[],
        mainline_tool_uses=(),
        round_idx=0,
        trace=_trace(),
        artifact_store=store,
    )
    new_keys = result.extra["produced_artifact_keys"]
    assert len(new_keys) == 2
    assert all(k.startswith("steelman/") for k in new_keys)
    suffixes = sorted(k.split("/")[-1] for k in new_keys)
    assert suffixes == ["side_a", "side_b"]


@pytest.mark.asyncio
async def test_run_spawn_skips_empty_produces_text():
    """Empty-text produces entries don't produce announcements or store
    entries — no point announcing a key with no content.
    """
    sub = _FakeSub(
        name="freeform",
        description="d",
        canned_result=SubroutineResult(
            text_summary="summary",
            produces={"": "", "extra": "real content"},
        ),
    )
    orch = _orch_with(sub)
    store = ArtifactStore()
    clock = BudgetClock(spec=BudgetSpec(max_tokens=1_000_000))
    tu = _spawn_tu("freeform", intent="x")
    result = await orch._run_spawn(
        tu,
        call_id="c-1",
        question_id="q-1",
        clock=clock,
        mainline_system_prompt="",
        mainline_messages=[],
        mainline_tool_uses=(),
        round_idx=0,
        trace=_trace(),
        artifact_store=store,
    )
    new_keys = result.extra["produced_artifact_keys"]
    assert len(new_keys) == 1
    assert new_keys[0].endswith("/extra")


@pytest.mark.asyncio
async def test_run_spawn_appends_announcement_to_text_summary():
    """The spawn's text_summary gets per-key announcements appended so
    mainline sees the new keys in its next turn (no separate registry
    block per round).
    """
    sub = _FakeSub(
        name="pair_notes",
        description="d",
        canned_result=SubroutineResult(
            text_summary="summary text",
            produces={"": "the body"},
        ),
    )
    orch = _orch_with(sub)
    store = ArtifactStore()
    clock = BudgetClock(spec=BudgetSpec(max_tokens=1_000_000))
    tu = _spawn_tu("pair_notes", intent="x")
    result = await orch._run_spawn(
        tu,
        call_id="c-1",
        question_id="q-1",
        clock=clock,
        mainline_system_prompt="",
        mainline_messages=[],
        mainline_tool_uses=(),
        round_idx=0,
        trace=_trace(),
        artifact_store=store,
    )
    assert "Produced artifact `pair_notes/" in result.text_summary
    assert "8 chars" in result.text_summary  # "the body" is 8 chars


@pytest.mark.asyncio
async def test_run_spawn_threads_artifacts_into_spawn_ctx():
    """The SpawnCtx the subroutine sees must carry the run's ArtifactStore
    and the validated include_artifacts so consumes splicing works.
    """
    received: dict[str, object] = {}

    @dataclass(frozen=True, kw_only=True)
    class _CapturingSub(SubroutineBase):
        async def run(self, ctx: SpawnCtx, overrides: Mapping[str, object]) -> SubroutineResult:
            received["artifacts"] = ctx.artifacts
            received["include_artifacts"] = ctx.include_artifacts
            return SubroutineResult(text_summary="ok", produces={})

        def fingerprint(self) -> Mapping[str, str | int | bool | None | list[str]]:
            out = dict(super().fingerprint())
            out["kind"] = "capturing"
            return out

    sub = _CapturingSub(name="cap", description="d")
    orch = _orch_with(sub)
    store = ArtifactStore(seed={"pair_text": "P", "rubric": "R"})
    clock = BudgetClock(spec=BudgetSpec(max_tokens=1_000_000))
    tu = _spawn_tu("cap", intent="x", include_artifacts=["pair_text"])
    await orch._run_spawn(
        tu,
        call_id="c-1",
        question_id="q-1",
        clock=clock,
        mainline_system_prompt="",
        mainline_messages=[],
        mainline_tool_uses=(),
        round_idx=0,
        trace=_trace(),
        artifact_store=store,
    )
    assert received["artifacts"] is store
    assert received["include_artifacts"] == ("pair_text",)
