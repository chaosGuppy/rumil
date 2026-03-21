"""Tests for dispatch type validation."""

from rumil.calls.dispatches import DISPATCH_DEFS
from rumil.models import (
    AssessDispatchPayload,
    CallType,
    Dispatch,
    DISPATCHABLE_CALL_TYPES,
    PrioritizationDispatchPayload,
    ScoutDispatchPayload,
    FindConsiderationsMode,
)
from rumil.calls.page_creators import _resolve_round_mode


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


def test_each_dispatch_def_schema_has_question_id():
    for ddef in DISPATCH_DEFS.values():
        schema = ddef.schema.model_json_schema()
        assert "question_id" in schema["properties"]
