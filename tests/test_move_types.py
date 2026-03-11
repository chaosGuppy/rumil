"""Tests for MoveType enum, move definitions, and move execution."""

from differential.models import LinkType, MoveType, PageType
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


async def test_create_claim_via_bind(tmp_db, scout_call):
    """Calling a bound CREATE_CLAIM tool should create a claim page in the DB."""
    state = MoveState(scout_call, tmp_db)
    tool = MOVES[MoveType.CREATE_CLAIM].bind(state)
    await tool.fn({"summary": "Sky is blue", "content": "The sky appears blue."})

    assert len(state.created_page_ids) == 1
    page = await tmp_db.get_page(state.created_page_ids[0])
    assert page is not None
    assert page.page_type is PageType.CLAIM
    assert page.summary == "Sky is blue"


async def test_load_page_creates_nothing(tmp_db, scout_call):
    """Calling a bound LOAD_PAGE tool should not create any pages."""
    state = MoveState(scout_call, tmp_db)
    tool = MOVES[MoveType.LOAD_PAGE].bind(state)
    await tool.fn({"page_id": "abc12345"})

    assert state.created_page_ids == []


async def test_inline_consideration_link(tmp_db, scout_call, question_page):
    """create_claim with links should create the page and consideration links."""
    state = MoveState(scout_call, tmp_db)
    tool = MOVES[MoveType.CREATE_CLAIM].bind(state)
    await tool.fn({
        "summary": "Sky scatters blue light",
        "content": "Rayleigh scattering causes blue wavelengths to dominate.",
        "links": [{
            "question_id": question_page.id[:8],
            "direction": "supports",
            "strength": 4.0,
            "reasoning": "Direct evidence for blue sky",
        }],
    })

    assert len(state.created_page_ids) == 1
    claim_id = state.created_page_ids[0]
    links = await tmp_db.get_links_from(claim_id)
    assert len(links) == 1
    assert links[0].link_type == LinkType.CONSIDERATION
    assert links[0].to_page_id == question_page.id
    assert links[0].strength == 4.0


async def test_inline_child_question_link(tmp_db, scout_call, question_page):
    """create_question with links should create the page and child_question links."""
    state = MoveState(scout_call, tmp_db)
    tool = MOVES[MoveType.CREATE_QUESTION].bind(state)
    await tool.fn({
        "summary": "What wavelengths does the atmosphere scatter?",
        "content": "Sub-question about atmospheric scattering.",
        "links": [{
            "parent_id": question_page.id[:8],
            "reasoning": "Decomposition of blue sky question",
        }],
    })

    assert len(state.created_page_ids) == 1
    child_id = state.created_page_ids[0]
    links = await tmp_db.get_links_to(child_id)
    assert len(links) == 1
    assert links[0].link_type == LinkType.CHILD_QUESTION
    assert links[0].from_page_id == question_page.id


async def test_no_links_creates_no_links(tmp_db, scout_call):
    """create_claim without links should not create any links."""
    state = MoveState(scout_call, tmp_db)
    tool = MOVES[MoveType.CREATE_CLAIM].bind(state)
    await tool.fn({
        "summary": "Unlinked claim",
        "content": "This claim is not linked.",
    })

    assert len(state.created_page_ids) == 1
    claim_id = state.created_page_ids[0]
    links = await tmp_db.get_links_from(claim_id)
    assert len(links) == 0
