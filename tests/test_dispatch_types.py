"""Tests for dispatch type validation."""

import pytest

from rumil.calls.dispatches import DISPATCH_DEFS, filter_mode_schema, make_mode_validator
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


def test_filter_mode_schema_dispatch_tool():
    """filter_mode_schema restricts the mode enum in a dispatch tool schema."""
    ddef = DISPATCH_DEFS[CallType.FIND_CONSIDERATIONS]
    schema = filter_mode_schema(
        ddef.schema.model_json_schema(),
        [FindConsiderationsMode.CONCRETE],
    )
    mode_enum = schema['$defs']['FindConsiderationsMode']['enum']
    assert mode_enum == ['concrete']
    mode_prop = schema['properties']['mode']
    assert mode_prop['default'] == 'concrete'


@pytest.mark.asyncio
async def test_mode_validator_rejects_disallowed():
    """The mode validator rejects a dispatch with a disallowed mode."""
    state = MoveState.__new__(MoveState)
    state.dispatches = []
    state._dispatch_validators = [
        make_mode_validator([FindConsiderationsMode.CONCRETE]),
    ]

    dispatch = Dispatch(
        call_type=CallType.FIND_CONSIDERATIONS,
        payload=ScoutDispatchPayload(
            question_id='abc', mode=FindConsiderationsMode.ABSTRACT, reason='test',
        ),
    )
    error = state.record_dispatch(dispatch)
    assert error is not None
    assert 'Invalid mode' in error
    assert len(state.dispatches) == 0


@pytest.mark.asyncio
async def test_mode_validator_accepts_allowed():
    """The mode validator accepts a dispatch with an allowed mode."""
    state = MoveState.__new__(MoveState)
    state.dispatches = []
    state._dispatch_validators = [
        make_mode_validator([FindConsiderationsMode.CONCRETE]),
    ]

    dispatch = Dispatch(
        call_type=CallType.FIND_CONSIDERATIONS,
        payload=ScoutDispatchPayload(
            question_id='abc', mode=FindConsiderationsMode.CONCRETE, reason='test',
        ),
    )
    error = state.record_dispatch(dispatch)
    assert error is None
    assert len(state.dispatches) == 1


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


def test_filter_mode_schema_nested():
    """filter_mode_schema restricts FindConsiderationsMode in nested $defs."""
    from rumil.moves.create_question import CreateSubquestionPayload

    schema = CreateSubquestionPayload.model_json_schema()
    filtered = filter_mode_schema(schema, [FindConsiderationsMode.ABSTRACT])

    mode_enum = filtered['$defs']['FindConsiderationsMode']['enum']
    assert mode_enum == ['abstract']
    inline_scout = filtered['$defs']['InlineScoutDispatch']
    assert inline_scout['properties']['mode']['default'] == 'abstract'


def test_mode_validator_passes_through_non_fc_dispatches():
    """The mode validator ignores non-find_considerations dispatches."""
    state = MoveState.__new__(MoveState)
    state.dispatches = []
    state._dispatch_validators = [
        make_mode_validator([FindConsiderationsMode.CONCRETE]),
    ]

    dispatch = Dispatch(
        call_type=CallType.ASSESS,
        payload=AssessDispatchPayload(question_id='abc', reason='test'),
    )
    error = state.record_dispatch(dispatch)
    assert error is None
    assert len(state.dispatches) == 1
