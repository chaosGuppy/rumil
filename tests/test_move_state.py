"""Tests for MoveState.take_new_moves drain behaviour."""

from rumil.models import Move, MoveType
from rumil.moves.base import MoveState


def _append_move(state: MoveState, move_type: MoveType = MoveType.LOAD_PAGE):
    """Simulate what MoveDef.bind does when a tool executes."""
    state.moves.append(Move(move_type=move_type, payload={}))
    state.move_created_ids.append([])
    state.move_trace_extras.append({})


async def test_take_new_moves_returns_moves_since_last_drain(tmp_db, scout_call):
    """take_new_moves returns only moves added since the previous call."""
    state = MoveState(scout_call, tmp_db)

    _append_move(state)
    _append_move(state)
    moves_r1, created_r1, extras_r1 = state.take_new_moves()
    assert len(moves_r1) == 2
    assert len(created_r1) == 2
    assert len(extras_r1) == 2

    _append_move(state)
    moves_r2, created_r2, extras_r2 = state.take_new_moves()
    assert len(moves_r2) == 1
    assert len(created_r2) == 1
    assert len(extras_r2) == 1

    assert len(state.moves) == 3


async def test_take_new_moves_returns_empty_when_no_new_moves(tmp_db, scout_call):
    """Consecutive drains with no new moves return empty lists."""
    state = MoveState(scout_call, tmp_db)

    _append_move(state)
    state.take_new_moves()

    moves, created, extras = state.take_new_moves()
    assert moves == []
    assert created == []
    assert extras == []


async def test_take_new_moves_works_from_nonzero_start(tmp_db, scout_call):
    """Moves added before the first drain are included in the first drain."""
    state = MoveState(scout_call, tmp_db)

    _append_move(state, MoveType.CREATE_CLAIM)
    _append_move(state, MoveType.LOAD_PAGE)
    _append_move(state, MoveType.CREATE_QUESTION)

    moves, _, _ = state.take_new_moves()
    assert [m.move_type for m in moves] == [
        MoveType.CREATE_CLAIM, MoveType.LOAD_PAGE, MoveType.CREATE_QUESTION,
    ]

    moves2, _, _ = state.take_new_moves()
    assert moves2 == []
