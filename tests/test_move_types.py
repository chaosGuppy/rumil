"""Tests for MoveType enum, move definitions, and move execution."""

from differential.models import MoveType, PageType
from differential.moves import MOVES
from differential.moves.base import MoveState


def test_all_move_types_have_definitions():
    """Every MoveType has a corresponding MoveDef in the MOVES registry."""
    for mt in MoveType:
        assert mt in MOVES, f"No MoveDef for {mt.value}"


def test_move_defs_have_valid_schemas():
    """Every MoveDef produces a valid JSON schema with properties."""
    for mt, move_def in MOVES.items():
        schema = move_def.schema.model_json_schema()
        assert "properties" in schema, f"{mt.value} schema missing properties"
        assert isinstance(schema["properties"], dict)


def test_create_claim_via_bind(tmp_db, scout_call):
    """Calling a bound CREATE_CLAIM tool should create a claim page in the DB."""
    state = MoveState(scout_call, tmp_db)
    tool = MOVES[MoveType.CREATE_CLAIM].bind(state)
    tool.fn({"summary": "Sky is blue", "content": "The sky appears blue."})

    assert len(state.created_page_ids) == 1
    page = tmp_db.get_page(state.created_page_ids[0])
    assert page is not None
    assert page.page_type is PageType.CLAIM
    assert page.summary == "Sky is blue"


def test_load_page_creates_nothing(tmp_db, scout_call):
    """Calling a bound LOAD_PAGE tool should not create any pages."""
    state = MoveState(scout_call, tmp_db)
    tool = MOVES[MoveType.LOAD_PAGE].bind(state)
    tool.fn({"page_id": "abc12345"})

    assert state.created_page_ids == []
