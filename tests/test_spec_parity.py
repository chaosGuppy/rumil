"""Contract: every spec-registered CallType produces the same task
description + allowed moves as its legacy imperative subclass. If these
diverge we've mis-translated the spec and shouldn't flip the registry
over to SpecCallRunner for that call type yet.
"""

from __future__ import annotations

import pytest

from rumil.calls import spec_registry  # noqa: F401  (side-effect)
from rumil.calls.call_registry import CALL_RUNNER_CLASSES
from rumil.calls.spec import SPECS, SpecKey
from rumil.calls.spec_runner import SpecCallRunner
from rumil.models import Call, CallStatus, CallType, Workspace


def _spec_keys() -> list[SpecKey]:
    keys: list[SpecKey] = list(SPECS.keys())
    keys.sort(key=lambda k: k[0].value)
    return keys


@pytest.fixture(params=_spec_keys())
async def spec_and_legacy(request, tmp_db, question_page):
    param: tuple[CallType, str] = request.param
    call_type, variant = param
    legacy_cls = CALL_RUNNER_CLASSES.get(call_type)
    if legacy_cls is None:
        pytest.skip(f"no legacy class for {call_type.value}")

    call = Call(
        call_type=call_type,
        workspace=Workspace.RESEARCH,
        scope_page_id=question_page.id,
        status=CallStatus.PENDING,
    )
    await tmp_db.save_call(call)

    legacy = legacy_cls(
        question_id=question_page.id,
        call=call,
        db=tmp_db,
    )

    spec = SPECS[(call_type, variant)]
    spec_runner = SpecCallRunner(
        spec,
        question_id=question_page.id,
        call=call,
        db=tmp_db,
    )
    return call_type, legacy, spec_runner


async def test_task_description_matches(spec_and_legacy):
    call_type, legacy, spec_runner = spec_and_legacy
    legacy_desc = legacy.task_description().strip()
    spec_desc = spec_runner.task_description().strip()
    assert legacy_desc == spec_desc, (
        f"{call_type.value}: legacy vs spec descriptions diverge\n"
        f"--- legacy ---\n{legacy_desc}\n--- spec ---\n{spec_desc}"
    )


async def test_allowed_moves_match(spec_and_legacy):
    call_type, legacy, spec_runner = spec_and_legacy
    legacy_moves = list(legacy._resolve_available_moves())
    spec_moves = list(spec_runner._resolve_available_moves())
    assert set(legacy_moves) == set(spec_moves), (
        f"{call_type.value}: legacy vs spec allowed-moves diverge\n"
        f"legacy_only: {set(legacy_moves) - set(spec_moves)}\n"
        f"spec_only:   {set(spec_moves) - set(legacy_moves)}"
    )
