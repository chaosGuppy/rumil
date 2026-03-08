"""Tests for dispatch type parsing and validation."""

from differential.models import DISPATCHABLE_CALL_TYPES, CallType
from differential.parser import Dispatch, parse_output


def test_known_dispatch_type_parsed_as_enum():
    raw = '<dispatch type="scout">{"question_id": "abc", "reason": "test"}</dispatch>'
    result = parse_output(raw)
    assert len(result.dispatches) == 1
    assert result.dispatches[0].call_type is CallType.SCOUT


def test_all_dispatchable_types_round_trip():
    for ct in DISPATCHABLE_CALL_TYPES:
        raw = f'<dispatch type="{ct.value}">{{"question_id": "abc"}}</dispatch>'
        result = parse_output(raw)
        assert len(result.dispatches) == 1, f"Failed to parse dispatch type {ct.value}"
        assert result.dispatches[0].call_type is ct


def test_unknown_dispatch_type_skipped():
    raw = '<dispatch type="explode">{"question_id": "abc"}</dispatch>'
    result = parse_output(raw)
    assert len(result.dispatches) == 0


def test_non_dispatchable_call_type_skipped():
    """A valid CallType that isn't dispatchable should be rejected."""
    assert CallType.INGEST not in DISPATCHABLE_CALL_TYPES
    raw = '<dispatch type="ingest">{"question_id": "abc"}</dispatch>'
    result = parse_output(raw)
    assert len(result.dispatches) == 0


def test_invalid_dispatch_does_not_block_valid_ones():
    raw = (
        '<dispatch type="explode">{"question_id": "abc"}</dispatch>'
        '<dispatch type="assess">{"question_id": "def", "reason": "ready"}</dispatch>'
    )
    result = parse_output(raw)
    assert len(result.dispatches) == 1
    assert result.dispatches[0].call_type is CallType.ASSESS


def test_dispatch_payload_accessible():
    raw = (
        '<dispatch type="scout">'
        '{"question_id": "abc123", "fruit_threshold": 3, "max_rounds": 8, "reason": "underexplored"}'
        "</dispatch>"
    )
    result = parse_output(raw)
    d = result.dispatches[0]
    assert d.call_type is CallType.SCOUT
    assert d.payload["question_id"] == "abc123"
    assert d.payload["fruit_threshold"] == 3
    assert d.payload["max_rounds"] == 8


def test_dispatch_is_dataclass_not_dict():
    """Dispatches should be Dispatch objects, not raw dicts."""
    raw = '<dispatch type="scout">{"question_id": "abc"}</dispatch>'
    result = parse_output(raw)
    assert isinstance(result.dispatches[0], Dispatch)
