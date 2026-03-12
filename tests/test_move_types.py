"""Tests for MoveType enum, move definitions, and move execution."""

from differential.models import CallType, LinkType, MoveType, PageType
from differential.moves import MOVES
from differential.moves.base import MoveState
from differential.moves.create_question import MOVE as CREATE_QUESTION_MOVE
from differential.moves.create_question import PRIORITIZATION_MOVE


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


async def test_inline_dispatch_on_create_subquestion(
    tmp_db, prioritization_call, question_page,
):
    """create_subquestion with dispatches should populate state.dispatches."""
    state = MoveState(prioritization_call, tmp_db)
    tool = PRIORITIZATION_MOVE.bind(state)
    await tool.fn({
        "summary": "What causes atmospheric scattering?",
        "content": "Sub-question about light scattering mechanisms.",
        "links": [{
            "parent_id": question_page.id[:8],
            "reasoning": "Decomposition of main question",
        }],
        "dispatches": [{
            "call_type": "scout",
            "reason": "Need initial evidence",
            "max_rounds": 3,
        }],
    })

    assert len(state.created_page_ids) == 1
    assert len(state.dispatches) == 1
    d = state.dispatches[0]
    assert d.call_type is CallType.SCOUT
    assert d.payload.question_id == state.created_page_ids[0]


async def test_inline_dispatch_multiple_types(
    tmp_db, prioritization_call, question_page,
):
    """create_subquestion with multiple dispatch types should create all."""
    state = MoveState(prioritization_call, tmp_db)
    tool = PRIORITIZATION_MOVE.bind(state)
    await tool.fn({
        "summary": "What is the role of wavelength in scattering?",
        "content": "Investigating wavelength dependence.",
        "links": [{
            "parent_id": question_page.id[:8],
            "reasoning": "Decomposition",
        }],
        "dispatches": [
            {"call_type": "scout", "reason": "Explore", "max_rounds": 2},
            {"call_type": "assess", "reason": "Evaluate"},
        ],
    })

    assert len(state.dispatches) == 2
    assert state.dispatches[0].call_type is CallType.SCOUT
    assert state.dispatches[1].call_type is CallType.ASSESS
    for d in state.dispatches:
        assert d.payload.question_id == state.created_page_ids[0]


async def test_no_dispatches_creates_no_dispatches(tmp_db, prioritization_call):
    """create_subquestion without dispatches should still create the question."""
    state = MoveState(prioritization_call, tmp_db)
    tool = PRIORITIZATION_MOVE.bind(state)
    await tool.fn({
        "summary": "A plain subquestion",
        "content": "No dispatches here.",
    })

    assert len(state.created_page_ids) == 1
    assert len(state.dispatches) == 0
    page = await tmp_db.get_page(state.created_page_ids[0])
    assert page is not None
    assert page.page_type is PageType.QUESTION


def test_create_subquestion_schema_includes_dispatches():
    """PRIORITIZATION_MOVE schema has dispatches; MOVE schema does not."""
    sub_schema = PRIORITIZATION_MOVE.schema.model_json_schema()
    assert "dispatches" in sub_schema["properties"]

    base_schema = CREATE_QUESTION_MOVE.schema.model_json_schema()
    assert "dispatches" not in base_schema["properties"]
