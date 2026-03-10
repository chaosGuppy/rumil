"""Tests for dispatch type validation."""

from differential.models import DISPATCHABLE_CALL_TYPES, CallType, Dispatch
from differential.calls.common import DispatchPayload


def test_dispatchable_types_include_expected():
    assert CallType.SCOUT in DISPATCHABLE_CALL_TYPES
    assert CallType.ASSESS in DISPATCHABLE_CALL_TYPES
    assert CallType.PRIORITIZATION in DISPATCHABLE_CALL_TYPES


def test_ingest_not_dispatchable():
    assert CallType.INGEST not in DISPATCHABLE_CALL_TYPES


def test_dispatch_holds_call_type_and_payload():
    d = Dispatch(
        call_type=CallType.SCOUT,
        payload={"question_id": "abc", "reason": "test"},
    )
    assert d.call_type is CallType.SCOUT
    assert d.payload["question_id"] == "abc"


def test_dispatch_payload_schema_has_required_fields():
    schema = DispatchPayload.model_json_schema()
    assert "call_type" in schema["properties"]
    assert "question_id" in schema["properties"]
