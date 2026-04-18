from typing import cast

from pydantic import BaseModel

from rumil.database import DB
from rumil.models import (
    AssessDispatchPayload,
    Call,
    CallType,
    Dispatch,
    Move,
    MoveType,
)
from rumil.moves.base import MoveEffect, MoveEntry, MoveResult, MoveState


class _StubPayload(BaseModel):
    pass


def _make_state() -> MoveState:
    return MoveState(cast(Call, None), cast(DB, None))


def _make_move(move_type: MoveType = MoveType.CREATE_CLAIM) -> Move:
    return Move(move_type=move_type, payload=_StubPayload())


def _make_entry(primary_id: str, move_type: MoveType = MoveType.CREATE_CLAIM) -> MoveEntry:
    return MoveEntry(move=_make_move(move_type), primary_id=primary_id)


def _make_dispatch(question_id: str = "q-1") -> Dispatch:
    return Dispatch(
        call_type=CallType.ASSESS,
        payload=AssessDispatchPayload(question_id=question_id),
    )


def test_monoid_identity():
    eff = MoveEffect(entries=(_make_entry("page-a"),))
    assert MoveEffect.empty() + eff == eff
    assert eff + MoveEffect.empty() == eff


def test_monoid_associativity():
    a = MoveEffect(entries=(_make_entry("page-a"),))
    b = MoveEffect(entries=(_make_entry("page-b", MoveType.CREATE_QUESTION),))
    c = MoveEffect(entries=(_make_entry("page-c"), _make_entry("page-d")))
    assert (a + b) + c == a + (b + c)


def test_apply_with_no_validators_mutates_state():
    state = _make_state()
    entry1 = _make_entry("page-1")
    entry2 = MoveEntry(
        move=_make_move(MoveType.CREATE_QUESTION),
        primary_id="page-2",
        extra_ids=("page-2-extra",),
        trace_extra={"note": "hi"},
    )
    effect = MoveEffect(entries=(entry1, entry2))

    result = state.apply(effect)

    assert result is None
    assert [m.move_type for m in state.moves] == [
        MoveType.CREATE_CLAIM,
        MoveType.CREATE_QUESTION,
    ]
    assert state.created_page_ids == ["page-1", "page-2"]
    assert state.last_created_id == "page-2"
    assert state.context_page_ids == {"page-1", "page-2"}
    assert state.move_created_ids == [["page-1"], ["page-2", "page-2-extra"]]
    assert state.move_trace_extras == [{}, {"note": "hi"}]


def test_apply_trace_extra_is_copied_not_shared():
    state = _make_state()
    shared = {"k": "original"}
    entry = MoveEntry(move=_make_move(), primary_id="p", trace_extra=shared)
    state.apply(MoveEffect(entries=(entry,)))
    shared["k"] = "mutated"
    assert state.move_trace_extras == [{"k": "original"}]


def test_effect_validator_rejects_batch_leaves_state_untouched():
    state = _make_state()
    state._effect_validators.append(lambda eff: "too many entries" if len(eff.entries) > 1 else eff)

    composed = MoveEffect(entries=(_make_entry("page-1"),)) + MoveEffect(
        entries=(_make_entry("page-2"),)
    )
    result = state.apply(composed)

    assert result == "too many entries"
    assert state.moves == []
    assert state.created_page_ids == []
    assert state.last_created_id is None
    assert state.context_page_ids == set()
    assert state.move_created_ids == []
    assert state.move_trace_extras == []
    assert state.dispatches == []


def test_apply_records_dispatches_in_per_move_order():
    state = _make_state()
    d1 = _make_dispatch("q-1")
    d2 = _make_dispatch("q-2")
    d3 = _make_dispatch("q-3")
    entry1 = MoveEntry(move=_make_move(), primary_id="page-1", dispatches=(d1, d2))
    entry2 = MoveEntry(move=_make_move(), primary_id="page-2", dispatches=(d3,))
    state.apply(MoveEffect(entries=(entry1, entry2)))
    assert state.dispatches == [d1, d2, d3]


def test_effect_dispatches_property_flattens_across_entries():
    d1 = _make_dispatch("q-1")
    d2 = _make_dispatch("q-2")
    entry1 = MoveEntry(move=_make_move(), primary_id="p1", dispatches=(d1,))
    entry2 = MoveEntry(move=_make_move(), primary_id="p2", dispatches=(d2,))
    eff = MoveEffect(entries=(entry1, entry2))
    assert eff.dispatches == (d1, d2)


def test_from_result_roundtrip():
    move = _make_move()
    d = _make_dispatch()
    res = MoveResult(
        message="ok",
        created_page_id="page-1",
        extra_created_ids=["extra-1", "extra-2"],
        trace_extra={"k": "v"},
        dispatches=[d],
    )
    eff = MoveEffect.from_result(move, res)
    assert len(eff.entries) == 1
    entry = eff.entries[0]
    assert entry.move is move
    assert entry.primary_id == "page-1"
    assert entry.extra_ids == ("extra-1", "extra-2")
    assert entry.trace_extra == {"k": "v"}
    assert entry.dispatches == (d,)
    assert eff.primary_ids == ("page-1",)
    assert eff.dispatches == (d,)
