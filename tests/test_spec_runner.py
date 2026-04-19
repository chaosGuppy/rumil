"""SpecCallRunner resolves a CallSpec's stages through the registries
and honors task_description + allowed-moves resolution.

Does not LLM-call — these are wiring tests. The registered scout_analogies
spec (in ``rumil.calls.spec_registry``) is what the runner is driven
from; the lifecycle test suite exercises the imperative equivalent.
"""

from __future__ import annotations

from rumil.calls import spec_registry  # noqa: F401  (side-effect import)
from rumil.calls.closing_reviewers import StandardClosingReview
from rumil.calls.context_builders import EmbeddingContext
from rumil.calls.page_creators import MultiRoundLoop
from rumil.calls.spec import SPECS
from rumil.calls.spec_runner import SpecCallRunner
from rumil.models import Call, CallStatus, CallType, Workspace


async def test_scout_analogies_spec_registered():
    spec = SPECS[(CallType.SCOUT_ANALOGIES, "default")]
    assert spec.call_type == CallType.SCOUT_ANALOGIES
    assert spec.prompt_id == "scout_analogies"
    assert spec.context_builder.id == "embedding"
    assert spec.workspace_updater.id == "multi_round_loop"
    assert spec.closing_reviewer.id == "standard_review"


async def test_spec_runner_resolves_stage_types(tmp_db, question_page):
    spec = SPECS[(CallType.SCOUT_ANALOGIES, "default")]
    call = Call(
        call_type=CallType.SCOUT_ANALOGIES,
        workspace=Workspace.RESEARCH,
        scope_page_id=question_page.id,
        status=CallStatus.PENDING,
    )
    await tmp_db.save_call(call)

    runner = SpecCallRunner(
        spec,
        question_id=question_page.id,
        call=call,
        db=tmp_db,
        max_rounds=3,
        fruit_threshold=2,
    )

    assert isinstance(runner.context_builder, EmbeddingContext)
    assert isinstance(runner.workspace_updater, MultiRoundLoop)
    assert isinstance(runner.closing_reviewer, StandardClosingReview)


async def test_spec_runner_task_description_includes_question_id(tmp_db, question_page):
    spec = SPECS[(CallType.SCOUT_ANALOGIES, "default")]
    call = Call(
        call_type=CallType.SCOUT_ANALOGIES,
        workspace=Workspace.RESEARCH,
        scope_page_id=question_page.id,
        status=CallStatus.PENDING,
    )
    await tmp_db.save_call(call)

    runner = SpecCallRunner(
        spec,
        question_id=question_page.id,
        call=call,
        db=tmp_db,
    )

    desc = runner.task_description()
    assert spec.description.strip() in desc
    assert f"Question ID: `{question_page.id}`" in desc


async def test_spec_runner_resolves_allowed_moves(tmp_db, question_page):
    spec = SPECS[(CallType.SCOUT_ANALOGIES, "default")]
    call = Call(
        call_type=CallType.SCOUT_ANALOGIES,
        workspace=Workspace.RESEARCH,
        scope_page_id=question_page.id,
        status=CallStatus.PENDING,
    )
    await tmp_db.save_call(call)
    runner = SpecCallRunner(
        spec,
        question_id=question_page.id,
        call=call,
        db=tmp_db,
    )
    moves = runner._resolve_available_moves()
    assert len(moves) > 0
