"""Tests for dispatch type validation."""

from rumil.calls.dispatches import DISPATCH_DEFS
from rumil.models import (
    DISPATCHABLE_CALL_TYPES,
    AssessDispatchPayload,
    CallType,
    Dispatch,
    ScopeOnlyDispatchPayload,
    ScoutDispatchPayload,
    ScoutSubquestionsDispatchPayload,
)


def test_dispatchable_types_include_expected():
    assert CallType.FIND_CONSIDERATIONS in DISPATCHABLE_CALL_TYPES
    assert CallType.ASSESS in DISPATCHABLE_CALL_TYPES


def test_ingest_not_dispatchable():
    assert CallType.INGEST not in DISPATCHABLE_CALL_TYPES


def test_dispatch_defs_match_dispatchable_types():
    assert set(DISPATCH_DEFS.keys()) == DISPATCHABLE_CALL_TYPES


def test_dispatch_holds_typed_payload():
    payload = ScoutDispatchPayload(
        question_id="abc",
        reason="test",
    )
    d = Dispatch(call_type=CallType.FIND_CONSIDERATIONS, payload=payload)
    assert d.call_type is CallType.FIND_CONSIDERATIONS
    assert d.payload.question_id == "abc"


def test_scout_payload_has_defaults():
    p = ScoutDispatchPayload(question_id="abc")
    assert p.fruit_threshold == 4
    assert p.max_rounds == 5


def test_assess_payload_has_no_extras():
    p = AssessDispatchPayload(question_id="abc")
    assert p.question_id == "abc"
    assert p.reason == ""


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
    assert p.question_id == ""
    assert "question_id" not in p.model_json_schema().get("properties", {})
