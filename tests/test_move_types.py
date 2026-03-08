"""Tests for MoveType enum, parsing, and execution."""

from differential.models import MoveType, PageType
from differential.parser import parse_output


def test_known_move_type_parsed_as_enum():
    raw = '<move type="CREATE_CLAIM">{"summary": "test", "content": "test"}</move>'
    result = parse_output(raw)
    assert len(result.moves) == 1
    assert result.moves[0].move_type is MoveType.CREATE_CLAIM


def test_all_move_types_round_trip_through_parser():
    """Every MoveType variant can be parsed from LLM-style output."""
    for mt in MoveType:
        raw = f'<move type="{mt.value}">{{"page_id": "abc"}}</move>'
        result = parse_output(raw)
        assert len(result.moves) == 1, f"Failed to parse {mt.value}"
        assert result.moves[0].move_type is mt


def test_unknown_move_type_skipped():
    raw = '<move type="TOTALLY_FAKE">{"x": 1}</move>'
    result = parse_output(raw)
    assert len(result.moves) == 0


def test_unknown_move_type_does_not_block_valid_moves():
    raw = (
        '<move type="TOTALLY_FAKE">{"x": 1}</move>'
        '<move type="CREATE_CLAIM">{"summary": "real", "content": "real"}</move>'
    )
    result = parse_output(raw)
    assert len(result.moves) == 1
    assert result.moves[0].move_type is MoveType.CREATE_CLAIM


def test_case_insensitive_move_type_parsing():
    raw = '<move type="create_claim">{"summary": "test", "content": "test"}</move>'
    result = parse_output(raw)
    assert len(result.moves) == 1
    assert result.moves[0].move_type is MoveType.CREATE_CLAIM


def test_load_page_ids_extracted():
    raw = '<move type="LOAD_PAGE">{"page_id": "abc12345"}</move>'
    result = parse_output(raw)
    assert result.load_page_ids == ["abc12345"]
    assert result.moves[0].move_type is MoveType.LOAD_PAGE


def test_create_claim_produces_page_in_db(tmp_db, scout_call):
    raw = '<move type="CREATE_CLAIM">{"summary": "Sky is blue", "content": "The sky appears blue."}</move>'
    parsed = parse_output(raw)

    from differential.executor import execute_all_moves

    created_ids = execute_all_moves(parsed, scout_call, tmp_db)
    assert len(created_ids) == 1
    page = tmp_db.get_page(created_ids[0])
    assert page is not None
    assert page.page_type is PageType.CLAIM
    assert page.summary == "Sky is blue"


def test_load_page_move_creates_nothing_in_db(tmp_db, scout_call):
    raw = '<move type="LOAD_PAGE">{"page_id": "abc12345"}</move>'
    parsed = parse_output(raw)

    from differential.executor import execute_all_moves

    created_ids = execute_all_moves(parsed, scout_call, tmp_db)
    assert created_ids == []
