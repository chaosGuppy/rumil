"""Tests for ``DraftAndEditWorkflow``.

Heavy mocking everywhere — the workflow fires real LLM calls for the
drafter / N critics / editor per round; running it for real costs a few
dollars per pair. Tests focus on:

- Class-attr / protocol contract (``Workflow`` runtime-checkable,
  ``produces_artifact=True``, ``code_paths``, ``name``).
- Fingerprint shape and forking — every constructor knob must change
  the fingerprint, plus the introspection check from
  ``test_versus_workflow.py``'s pattern.
- Phase ordering: round 0 fires drafter then N critics in parallel;
  rounds 1+ fire editor then N critics, in that order.
- Budget accounting: 1 unit consumed per round at the top; if the
  budget exhausts before any draft is produced ``last_status`` flips
  to ``"incomplete"``.
- Final draft lands on ``question.content`` via
  ``db.update_page_content`` (mutation-event-aware path).
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call

import pytest

_VERSUS_SRC = Path(__file__).resolve().parents[1] / "versus" / "src"
if str(_VERSUS_SRC) not in sys.path:
    sys.path.insert(0, str(_VERSUS_SRC))

from rumil.orchestrators.draft_and_edit import (  # noqa: E402
    DraftAndEditWorkflow,
    _extract_continuation,
    _extract_prefix_from_question_body,
    _extract_target_length_chars,
)
from rumil.settings import override_settings  # noqa: E402


@pytest.fixture(autouse=True)
def _stub_rumil_model_override():
    """The workflow's ``_resolve_model`` requires either a per-role
    constructor override or ``rumil_model_override`` set in settings
    (the production ``run_versus`` path sets the latter). Tests
    construct workflows directly without per-role models; stub the
    override globally so ``_resolve_model`` doesn't fail-loud.
    """
    with override_settings(rumil_model_override="claude-sonnet-4-6"):
        yield


from rumil.versus_workflow import Workflow  # noqa: E402

_QUESTION_FRAMING_TEMPLATE = (
    "This question was created by the versus essay-completion harness. "
    "The goal of this run is to produce a high-quality continuation.\n\n"
    "## Essay opening\n\n{prefix}\n\n"
    "## Target length\n\nApproximately {target} characters.\n\n"
    "## Goal\n\nContinue the essay opening above."
)


def _make_question_framing(target: int = 2000, prefix: str = "Once upon a time...") -> str:
    return _QUESTION_FRAMING_TEMPLATE.format(target=target, prefix=prefix)


def _make_db(
    mocker,
    *,
    question_content: str | None = None,
    source_content: str = "Once upon a time...",
    budget: int = 100,
):
    """Build a fully mocked DB that mimics what the workflow touches.

    The workflow now reads the essay opening directly out of the
    Question's body (under a ``## Essay opening`` header). The mock
    Question is built with ``source_content`` inlined into that
    header so the workflow's prefix extractor finds it. Pass an
    explicit ``question_content`` to drive error-path tests with a
    malformed body.

    The mock tracks ``budget`` across ``consume_budget`` /
    ``budget_remaining`` calls so the workflow's "skip critique on
    final round" detection (which peeks ``budget_remaining``) sees
    realistic values. Default budget is large enough to be a no-op
    for tests that already cap rounds via ``max_rounds``.

    update_page_content writes back to the in-memory question so a test
    can read the persisted final-draft after run() returns.
    """
    db = MagicMock()
    db.run_id = "run-1"
    fake_question = MagicMock()
    fake_question.content = question_content or _make_question_framing(prefix=source_content)
    db.get_page = AsyncMock(return_value=fake_question)
    db.init_budget = AsyncMock()
    remaining = {"value": budget}

    async def _consume(amount: int = 1) -> bool:
        if remaining["value"] < amount:
            return False
        remaining["value"] -= amount
        return True

    async def _remaining() -> int:
        return max(0, remaining["value"])

    db.consume_budget = AsyncMock(side_effect=_consume)
    db.add_budget = AsyncMock()
    db.budget_remaining = AsyncMock(side_effect=_remaining)
    db.qbp_consume = AsyncMock()
    db.update_call_status = AsyncMock()
    db.save_call = AsyncMock()
    db.save_call_trace = AsyncMock()

    fake_call = MagicMock()
    fake_call.id = "call-de-1"
    fake_call.cost_usd = None
    db.create_call = AsyncMock(return_value=fake_call)

    async def _record_content(page_id: str, new_content: str) -> None:
        fake_question.content = new_content

    db.update_page_content = AsyncMock(side_effect=_record_content)
    return db, fake_question, fake_call


def _patch_text_call(mocker, *, drafter_text: str, critic_text: str, editor_text: str):
    """Patch ``rumil.orchestrators.draft_and_edit.text_call`` so each
    phase returns a deterministic string. The phase metadata threaded
    through ``LLMExchangeMetadata`` lets us route per-call.
    """

    async def _fake(system_prompt, user_message="", **kwargs):
        meta = kwargs.get("metadata")
        phase = meta.phase if meta is not None else ""
        if phase == "draft":
            return f"<continuation>{drafter_text}</continuation>"
        if phase.startswith("critic_"):
            return critic_text
        if phase.startswith("edit_"):
            return f"<continuation>{editor_text}</continuation>"
        return ""

    return mocker.patch(
        "rumil.orchestrators.draft_and_edit.text_call",
        new=AsyncMock(side_effect=_fake),
    )


def test_workflow_class_attrs():
    wf = DraftAndEditWorkflow(budget=4)
    assert wf.name == "draft_and_edit"
    assert wf.produces_artifact is True
    assert isinstance(wf.code_paths, tuple)
    assert "src/rumil/orchestrators/draft_and_edit.py" in wf.code_paths


def test_workflow_satisfies_runtime_protocol():
    assert isinstance(DraftAndEditWorkflow(budget=1), Workflow)


def test_constructor_validates_budget():
    with pytest.raises(ValueError, match="budget"):
        DraftAndEditWorkflow(budget=0)


def test_constructor_validates_n_critics():
    with pytest.raises(ValueError, match="n_critics"):
        DraftAndEditWorkflow(budget=4, n_critics=0)


def test_constructor_validates_max_rounds():
    with pytest.raises(ValueError, match="max_rounds"):
        DraftAndEditWorkflow(budget=4, max_rounds=0)


def test_constructor_max_rounds_none_is_valid():
    wf = DraftAndEditWorkflow(budget=4, max_rounds=None)
    assert wf.max_rounds is None


def test_fingerprint_includes_kind_and_budget():
    wf = DraftAndEditWorkflow(budget=7)
    fp = wf.fingerprint()
    assert fp["kind"] == "draft_and_edit"
    assert fp["budget"] == 7


def test_fingerprint_changes_on_budget():
    a = DraftAndEditWorkflow(budget=4).fingerprint()
    b = DraftAndEditWorkflow(budget=8).fingerprint()
    assert a != b


def test_fingerprint_changes_on_n_critics():
    a = DraftAndEditWorkflow(budget=4, n_critics=2).fingerprint()
    b = DraftAndEditWorkflow(budget=4, n_critics=3).fingerprint()
    assert a != b


def test_fingerprint_changes_on_max_rounds():
    a = DraftAndEditWorkflow(budget=4, max_rounds=2).fingerprint()
    b = DraftAndEditWorkflow(budget=4, max_rounds=5).fingerprint()
    assert a != b


def test_fingerprint_changes_on_drafter_model():
    a = DraftAndEditWorkflow(budget=4).fingerprint()
    b = DraftAndEditWorkflow(budget=4, drafter_model="claude-sonnet-4-6").fingerprint()
    assert a != b


def test_fingerprint_changes_on_critic_model():
    a = DraftAndEditWorkflow(budget=4).fingerprint()
    b = DraftAndEditWorkflow(budget=4, critic_model="claude-sonnet-4-6").fingerprint()
    assert a != b


def test_fingerprint_changes_on_editor_model():
    a = DraftAndEditWorkflow(budget=4).fingerprint()
    b = DraftAndEditWorkflow(budget=4, editor_model="claude-sonnet-4-6").fingerprint()
    assert a != b


def test_fingerprint_includes_prompt_hashes():
    fp = DraftAndEditWorkflow(budget=4).fingerprint()
    assert "drafter_prompt_hash" in fp
    assert "critic_prompt_hash" in fp
    assert "editor_prompt_hash" in fp
    # Each is an 8-hex sha256 prefix.
    for k in ("drafter_prompt_hash", "critic_prompt_hash", "editor_prompt_hash"):
        v = fp[k]
        assert isinstance(v, str)
        assert len(v) == 8
        int(v, 16)


_OPAQUE_TO_FINGERPRINT = {
    "name",
    "code_paths",
    "produces_artifact",
    "relevant_settings",
    "last_status",
    # Prompt path attrs are pure telemetry; identical content via
    # different paths fingerprints the same via *_prompt_hash.
    "drafter_prompt_path",
    "critic_prompt_path",
    "editor_prompt_path",
    "planner_prompt_path",
    "arbiter_prompt_path",
    "audit_prompt_path",
}


def _public_attrs(workflow: object) -> set[str]:
    out: set[str] = set()
    for attr in dir(workflow):
        if attr.startswith("_"):
            continue
        value = getattr(workflow, attr)
        if callable(value):
            continue
        out.add(attr)
    return out


def _fingerprint_keys_normalized(fp: Mapping[str, object]) -> set[str]:
    keys = {k.removeprefix("settings.").removesuffix("_hash") for k in fp}
    if "kind" in keys:
        keys.add("name")
    return keys


def test_workflow_fingerprint_covers_all_public_fields():
    """Catches drift: a new ``self.foo = ...`` knob in __init__ that's
    not folded into ``fingerprint()`` would silently let runs dedup
    against each other when they shouldn't."""
    # Enable every optional stage so its knobs (planner_*, arbiter_*,
    # audit_*) participate in fingerprint() — fingerprint folds them in
    # conditionally on the with_* flags.
    wf = DraftAndEditWorkflow(
        budget=4,
        n_critics=3,
        max_rounds=2,
        with_planner=True,
        with_arbiter=True,
        with_brief_audit=True,
    )
    fp = wf.fingerprint()
    public_attrs = _public_attrs(wf) - _OPAQUE_TO_FINGERPRINT
    fp_keys = _fingerprint_keys_normalized(fp)
    missing = public_attrs - fp_keys
    assert not missing, f"Workflow fields missing from fingerprint(): {missing}"


@pytest.mark.asyncio
async def test_setup_seeds_budget(mocker):
    db = MagicMock()
    db.init_budget = AsyncMock()
    wf = DraftAndEditWorkflow(budget=10)
    await wf.setup(db, "q-1")
    db.init_budget.assert_awaited_once_with(10)


@pytest.mark.asyncio
async def test_run_one_round_writes_final_draft_to_question_content(mocker):
    """budget=1, max_rounds=1: drafter fires once, no critics (final
    round → no editor will read them), no editor. The drafter's
    output lands on question.content."""
    db, question, _call = _make_db(mocker)
    fake_text_call = _patch_text_call(
        mocker,
        drafter_text="DRAFT_R0",
        critic_text="critic prose",
        editor_text="should-not-fire",
    )

    wf = DraftAndEditWorkflow(budget=1, n_critics=2, max_rounds=1)
    await wf.run(db, "q-1", broadcaster=None)

    assert wf.last_status == "complete"
    db.update_page_content.assert_awaited_once_with("q-1", "DRAFT_R0")
    assert question.content == "DRAFT_R0"
    # 1 drafter; no critics (round 0 is also the final round, so the
    # critique step is skipped); no editor.
    assert fake_text_call.await_count == 1
    phases = [c.kwargs["metadata"].phase for c in fake_text_call.await_args_list]
    assert phases.count("draft") == 1
    assert not any(p.startswith("critic") for p in phases)
    assert not any(p.startswith("edit") for p in phases)


@pytest.mark.asyncio
async def test_run_two_rounds_fires_editor_in_round_one(mocker):
    """budget=2, max_rounds=2: round 0 drafter+2 critics, round 1
    editor (final round → no critics fire). Final content is the
    editor's output."""
    db, question, _call = _make_db(mocker)
    fake_text_call = _patch_text_call(
        mocker,
        drafter_text="DRAFT_R0",
        critic_text="critic prose",
        editor_text="EDITED_R1",
    )

    wf = DraftAndEditWorkflow(budget=2, n_critics=2, max_rounds=2)
    await wf.run(db, "q-1", broadcaster=None)

    assert wf.last_status == "complete"
    db.update_page_content.assert_awaited_once_with("q-1", "EDITED_R1")
    assert question.content == "EDITED_R1"
    # 1 draft + 2 critics + 1 edit = 4 LLM calls. The round-1
    # critique would have been wasted (no round-2 editor to read it),
    # so it's skipped.
    assert fake_text_call.await_count == 4
    phases = [c.kwargs["metadata"].phase for c in fake_text_call.await_args_list]
    assert phases.count("draft") == 1
    assert sum(p.startswith("edit_r1") for p in phases) == 1
    assert sum(p.startswith("critic_r0") for p in phases) == 2
    assert not any(p.startswith("critic_r1") for p in phases)


@pytest.mark.asyncio
async def test_run_phases_in_order_per_round(mocker):
    """Within a round, drafter (or editor) must precede critics. Verify
    via the order of phases in the captured call sequence. With
    budget=3 / max_rounds=3, round 0 and round 1 each fire critics;
    round 2 (final) skips them.
    """
    db, _question, _call = _make_db(mocker)
    fake_text_call = _patch_text_call(
        mocker,
        drafter_text="d",
        critic_text="c",
        editor_text="e",
    )

    wf = DraftAndEditWorkflow(budget=3, n_critics=2, max_rounds=3)
    await wf.run(db, "q-1", broadcaster=None)

    phases = [c.kwargs["metadata"].phase for c in fake_text_call.await_args_list]
    # First phase in round 0 is the drafter; first phase in round 1 is the editor.
    assert phases[0] == "draft"
    # Round 0 critics come after the drafter.
    round0_critic_indices = [i for i, p in enumerate(phases) if p.startswith("critic_r0")]
    assert all(i > 0 for i in round0_critic_indices)
    # Round 1 editor comes before round 1 critics.
    edit_r1_idx = phases.index("edit_r1")
    round1_critic_indices = [i for i, p in enumerate(phases) if p.startswith("critic_r1")]
    assert all(i > edit_r1_idx for i in round1_critic_indices)
    # Round 2 (final) fires the editor but skips the critique step.
    assert "edit_r2" in phases
    assert not any(p.startswith("critic_r2") for p in phases)


@pytest.mark.asyncio
async def test_run_consumes_one_budget_unit_per_round(mocker):
    """Verify the round-as-budget-unit convention: ``consume_budget`` is
    called once per round, not once per LLM exchange."""
    db, _question, _call = _make_db(mocker)
    _patch_text_call(mocker, drafter_text="d", critic_text="c", editor_text="e")

    wf = DraftAndEditWorkflow(budget=3, n_critics=2, max_rounds=3)
    await wf.run(db, "q-1", broadcaster=None)

    # 3 rounds → 3 consume_budget calls, each amount=1.
    assert db.consume_budget.await_count == 3
    for c in db.consume_budget.await_args_list:
        assert c.args == (1,) or c.kwargs.get("amount") == 1


@pytest.mark.asyncio
async def test_run_marks_incomplete_when_budget_exhausts_before_first_draft(mocker):
    """If consume_budget returns False on the very first round, no
    draft is produced — last_status flips to ``incomplete`` and
    ``update_page_content`` is never called."""
    db, _question, _call = _make_db(mocker)
    db.consume_budget = AsyncMock(return_value=False)
    fake_text_call = _patch_text_call(
        mocker,
        drafter_text="d",
        critic_text="c",
        editor_text="e",
    )

    wf = DraftAndEditWorkflow(budget=1, max_rounds=2)
    await wf.run(db, "q-1", broadcaster=None)

    assert wf.last_status == "incomplete"
    fake_text_call.assert_not_awaited()
    db.update_page_content.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_completes_when_budget_exhausts_after_first_draft(mocker):
    """If consume_budget returns True for round 0 then False for round 1,
    we keep the round-0 draft as the final artifact and stay
    ``complete`` (not ``incomplete``)."""
    db, _question, _call = _make_db(mocker)
    # Round 0 succeeds; subsequent rounds get exhausted budget.
    db.consume_budget = AsyncMock(side_effect=[True, False])
    _patch_text_call(mocker, drafter_text="DRAFT_R0", critic_text="c", editor_text="e")

    wf = DraftAndEditWorkflow(budget=1, max_rounds=5)
    await wf.run(db, "q-1", broadcaster=None)

    assert wf.last_status == "complete"
    db.update_page_content.assert_awaited_once_with("q-1", "DRAFT_R0")


@pytest.mark.asyncio
async def test_run_critics_fire_in_parallel(mocker):
    """The N critics in one round run via ``asyncio.gather``: they
    don't await each other in sequence. Verify by tracking the
    overlap between critic-call entry and exit. Use budget=2 /
    max_rounds=2 so the round-0 critique step actually fires (the
    final round skips its critique).
    """
    db, _question, _call = _make_db(mocker)

    in_flight = 0
    max_in_flight = 0

    async def _fake(system_prompt, user_message="", **kwargs):
        nonlocal in_flight, max_in_flight
        meta = kwargs.get("metadata")
        phase = meta.phase if meta is not None else ""
        if phase.startswith("critic_"):
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            # Yield to let the other critic start.
            import asyncio

            await asyncio.sleep(0)
            in_flight -= 1
            return "critic prose"
        if phase == "draft":
            return "<continuation>d</continuation>"
        return "<continuation>e</continuation>"

    mocker.patch(
        "rumil.orchestrators.draft_and_edit.text_call",
        new=AsyncMock(side_effect=_fake),
    )

    wf = DraftAndEditWorkflow(budget=2, n_critics=3, max_rounds=2)
    await wf.run(db, "q-1", broadcaster=None)

    assert max_in_flight == 3, "critics did not run concurrently"


@pytest.mark.asyncio
async def test_run_passes_per_role_models_to_text_call(mocker):
    """When per-role model overrides are set, text_call sees the
    matching model for each phase."""
    db, _question, _call = _make_db(mocker)
    fake_text_call = _patch_text_call(mocker, drafter_text="d", critic_text="c", editor_text="e")

    wf = DraftAndEditWorkflow(
        budget=3,
        n_critics=2,
        max_rounds=3,
        drafter_model="model-A",
        critic_model="model-B",
        editor_model="model-C",
    )
    await wf.run(db, "q-1", broadcaster=None)

    by_phase: dict[str, list[str]] = {}
    for c in fake_text_call.await_args_list:
        phase = c.kwargs["metadata"].phase
        model = c.kwargs["model"]
        by_phase.setdefault(phase, []).append(model)
    assert by_phase["draft"] == ["model-A"]
    assert by_phase["edit_r1"] == ["model-C"]
    assert by_phase["edit_r2"] == ["model-C"]
    assert all(m == "model-B" for m in by_phase["critic_r0_n0"] + by_phase["critic_r0_n1"])
    assert all(m == "model-B" for m in by_phase["critic_r1_n0"] + by_phase["critic_r1_n1"])


@pytest.mark.asyncio
async def test_run_emits_draft_critique_edit_trace_events(mocker):
    """Trace event surface: round 0 emits a DraftEvent + one
    CritiqueRoundEvent (the round-0 critiques feed round-1's edit);
    round 1 emits an EditEvent. The final round skips its critique
    step, so for budget=2 / max_rounds=2 only one CritiqueRoundEvent
    fires.
    """
    db, _question, _call = _make_db(mocker)
    _patch_text_call(mocker, drafter_text="d", critic_text="c", editor_text="e")

    wf = DraftAndEditWorkflow(budget=2, n_critics=2, max_rounds=2)
    await wf.run(db, "q-1", broadcaster=None)

    # Each save_call_trace call gets one event payload.
    events = [args.args[1][0]["event"] for args in db.save_call_trace.await_args_list]
    assert "draft" in events
    assert events.count("critique_round") == 1
    assert "edit" in events


@pytest.mark.asyncio
async def test_run_raises_when_question_missing(mocker):
    db = MagicMock()
    db.run_id = "run-1"
    db.get_page = AsyncMock(return_value=None)

    wf = DraftAndEditWorkflow(budget=1)
    with pytest.raises(RuntimeError, match="missing"):
        await wf.run(db, "q-1", broadcaster=None)


@pytest.mark.asyncio
async def test_run_raises_when_question_body_lacks_prefix_block(mocker):
    """The workflow reads the prefix out of a ``## Essay opening`` /
    ``## Target length`` block in the question body. If the body is
    malformed (missing the block), the workflow must error out
    explicitly rather than running on an empty prefix.
    """
    db, _question, _call = _make_db(mocker, question_content="no essay opening header here")
    wf = DraftAndEditWorkflow(budget=1)
    with pytest.raises(ValueError, match=r"Essay opening"):
        await wf.run(db, "q-1", broadcaster=None)


def test_extract_continuation_pulls_tagged_block():
    text = "scratch\n<continuation>the real text</continuation>\nmore"
    assert _extract_continuation(text) == "the real text"


def test_extract_continuation_falls_back_to_full_text():
    assert _extract_continuation("untagged response  ") == "untagged response"


def test_extract_continuation_uses_last_block_when_multiple():
    text = "<continuation>first</continuation>\n<continuation>second</continuation>"
    assert _extract_continuation(text) == "second"


def test_extract_continuation_recovers_from_unclosed_tag():
    """max_tokens truncation lops off the closing ``</continuation>``.

    The fallback used to dump the scratch text that comes BEFORE the
    opening tag, hiding the partially-generated continuation entirely
    (and causing downstream rounds to critique an empty draft). It now
    returns everything after the opener so the partial output is
    salvageable.
    """
    text = (
        "Plan: outline three sections, hit target length, match opening voice.\n\n"
        "<continuation>The argument hinges on a single observation: refusal "
        "without character is fragile. The system that refuses to spread "
        "misinformation only because it has been told to refuse will, given "
        "sufficient prompting"
    )
    assert _extract_continuation(text).startswith("The argument hinges")
    assert "Plan: outline" not in _extract_continuation(text)
    assert _extract_continuation(text).endswith("sufficient prompting")


def test_extract_prefix_pulls_essay_opening_from_question_body():
    """The workflow scrapes the prefix back out of the Question body's
    ``## Essay opening`` / ``## Target length`` block.
    """
    body = _make_question_framing(prefix="My essay opens with this.")
    assert _extract_prefix_from_question_body(body) == "My essay opens with this."


def test_extract_target_length_pulls_int_from_content():
    content = _make_question_framing(target=1234)
    assert _extract_target_length_chars(content) == 1234


def test_extract_target_length_returns_none_when_absent():
    assert _extract_target_length_chars("no target hint here") is None


def test_workflow_registry_includes_draft_and_edit():
    """Registry wiring: the versus completion harness can find the
    workflow by name. Pinning this here avoids a regression where
    the registry import succeeds but the entry was dropped."""
    from versus.rumil_completion import WORKFLOW_REGISTRY

    assert "draft_and_edit" in WORKFLOW_REGISTRY
    cls, defaults = WORKFLOW_REGISTRY["draft_and_edit"]
    assert isinstance(defaults, dict)
    assert cls is DraftAndEditWorkflow


def test_make_workflow_and_task_constructs_draft_and_edit_pair():
    from versus.rumil_completion import _make_workflow_and_task

    workflow, task = _make_workflow_and_task("draft_and_edit", budget=4)
    assert workflow.name == "draft_and_edit"
    assert workflow.budget == 4
    assert workflow.produces_artifact is True
    assert task.name == "complete_essay"


# Suppress the unused-import warning from the call() helper that some
# editors pre-emptively strip — kept here as a marker that future tests
# in this file can use it without re-importing.
_ = call
