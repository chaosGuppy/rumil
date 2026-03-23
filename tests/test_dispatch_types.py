"""Tests for dispatch type validation."""

import pytest

from rumil.calls.dispatches import DISPATCH_DEFS
from rumil.models import (
    AssessDispatchPayload,
    CallType,
    Dispatch,
    DISPATCHABLE_CALL_TYPES,
    PrioritizationDispatchPayload,
    ScopeOnlyDispatchPayload,
    ScoutDispatchPayload,
    FindConsiderationsMode,
    ScoutSubquestionsDispatchPayload,
)
from rumil.calls.page_creators import _resolve_round_mode
from rumil.moves.base import MoveState
from rumil.settings import get_settings, override_settings


def test_dispatchable_types_include_expected():
    assert CallType.FIND_CONSIDERATIONS in DISPATCHABLE_CALL_TYPES
    assert CallType.ASSESS in DISPATCHABLE_CALL_TYPES
    assert CallType.PRIORITIZATION in DISPATCHABLE_CALL_TYPES


def test_ingest_not_dispatchable():
    assert CallType.INGEST not in DISPATCHABLE_CALL_TYPES


def test_dispatch_defs_match_dispatchable_types():
    assert set(DISPATCH_DEFS.keys()) == DISPATCHABLE_CALL_TYPES


def test_dispatch_holds_typed_payload():
    payload = ScoutDispatchPayload(question_id="abc", reason="test")
    d = Dispatch(call_type=CallType.FIND_CONSIDERATIONS, payload=payload)
    assert d.call_type is CallType.FIND_CONSIDERATIONS
    assert d.payload.question_id == "abc"


def test_scout_payload_has_defaults():
    p = ScoutDispatchPayload(question_id="abc")
    assert p.mode == FindConsiderationsMode.ALTERNATE
    assert p.fruit_threshold == 4
    assert p.max_rounds == 5


def test_scout_payload_accepts_mode():
    p = ScoutDispatchPayload(question_id="abc", mode=FindConsiderationsMode.CONCRETE)
    assert p.mode == FindConsiderationsMode.CONCRETE


def test_resolve_round_mode_alternate():
    assert _resolve_round_mode(FindConsiderationsMode.ALTERNATE, 0) == FindConsiderationsMode.ABSTRACT
    assert _resolve_round_mode(FindConsiderationsMode.ALTERNATE, 1) == FindConsiderationsMode.CONCRETE
    assert _resolve_round_mode(FindConsiderationsMode.ALTERNATE, 2) == FindConsiderationsMode.ABSTRACT
    assert _resolve_round_mode(FindConsiderationsMode.ALTERNATE, 3) == FindConsiderationsMode.CONCRETE


def test_resolve_round_mode_fixed():
    assert _resolve_round_mode(FindConsiderationsMode.ABSTRACT, 0) == FindConsiderationsMode.ABSTRACT
    assert _resolve_round_mode(FindConsiderationsMode.ABSTRACT, 1) == FindConsiderationsMode.ABSTRACT
    assert _resolve_round_mode(FindConsiderationsMode.CONCRETE, 0) == FindConsiderationsMode.CONCRETE
    assert _resolve_round_mode(FindConsiderationsMode.CONCRETE, 1) == FindConsiderationsMode.CONCRETE


def test_assess_payload_has_no_extras():
    p = AssessDispatchPayload(question_id="abc")
    assert p.question_id == "abc"
    assert p.reason == ""


def test_prioritization_payload_requires_budget():
    p = PrioritizationDispatchPayload(question_id="abc", budget=10)
    assert p.budget == 10


def test_targeted_dispatch_defs_have_question_id():
    for ct, ddef in DISPATCH_DEFS.items():
        schema = ddef.schema.model_json_schema()
        if issubclass(ddef.schema, ScopeOnlyDispatchPayload):
            assert "question_id" not in schema.get("properties", {}), (
                f"{ct.value} is scope-only but exposes question_id in schema"
            )
        else:
            assert "question_id" in schema["properties"], (
                f"{ct.value} should expose question_id in schema"
            )


def test_scope_only_payload_accepts_no_question_id():
    p = ScoutSubquestionsDispatchPayload(reason="test")
    assert p.question_id == ''
    assert "question_id" not in p.model_json_schema().get("properties", {})


def test_bind_allowed_modes_filters_schema():
    """bind() with allowed_modes restricts the mode enum in the tool schema."""
    ddef = DISPATCH_DEFS[CallType.FIND_CONSIDERATIONS]
    state = MoveState.__new__(MoveState)
    state.dispatches = []
    state.moves = []
    state.created_page_ids = []

    tool = ddef.bind(
        state,
        allowed_modes=[FindConsiderationsMode.CONCRETE],
    )
    schema = tool.input_schema
    mode_prop = schema['properties']['mode']
    assert mode_prop['enum'] == ['concrete']
    assert mode_prop['default'] == 'concrete'
    assert '$defs' not in schema


@pytest.mark.asyncio
async def test_bind_allowed_modes_rejects_disallowed():
    """The callback rejects a mode not in allowed_modes."""
    ddef = DISPATCH_DEFS[CallType.FIND_CONSIDERATIONS]
    state = MoveState.__new__(MoveState)
    state.dispatches = []
    state.moves = []
    state.created_page_ids = []

    tool = ddef.bind(
        state,
        allowed_modes=[FindConsiderationsMode.CONCRETE],
    )
    result = await tool.fn({
        'question_id': 'abc',
        'mode': 'abstract',
        'reason': 'test',
    })
    assert 'Invalid mode' in result
    assert len(state.dispatches) == 0


def test_allowed_find_considerations_modes_property():
    """Settings property parses comma-separated modes correctly."""
    with override_settings(find_considerations_modes='concrete,abstract'):
        modes = get_settings().allowed_find_considerations_modes
        assert list(modes) == [
            FindConsiderationsMode.CONCRETE,
            FindConsiderationsMode.ABSTRACT,
        ]


def test_allowed_find_considerations_modes_single():
    """Settings property handles a single mode."""
    with override_settings(find_considerations_modes='alternate'):
        modes = get_settings().allowed_find_considerations_modes
        assert list(modes) == [FindConsiderationsMode.ALTERNATE]
