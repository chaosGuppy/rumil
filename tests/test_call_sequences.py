"""Tests for call sequences — DB layer and orchestrator wiring."""

import pytest

from rumil.models import (
    AssessDispatchPayload,
    CallType,
    Dispatch,
    ScoutDispatchPayload,
)
from rumil.orchestrators import BaseOrchestrator
from rumil.tracing.tracer import CallTrace


class ScriptedOrchestrator(BaseOrchestrator):
    """Returns pre-scripted batches of dispatch sequences."""

    def __init__(self, db, sequences, call_id=None):
        super().__init__(db)
        self._sequences = list(sequences)
        self._call_id = call_id

    async def run(self, root_question_id):
        await self._setup()
        try:
            remaining = await self.db.budget_remaining()
            if remaining <= 0 or not self._sequences:
                return
            await self._run_sequences(
                self._sequences,
                root_question_id,
                self._call_id,
            )
        finally:
            await self._teardown()


def _scout(qid, **kw):
    return Dispatch(
        call_type=CallType.FIND_CONSIDERATIONS,
        payload=ScoutDispatchPayload(question_id=qid, max_rounds=1, **kw),
    )


def _assess(qid, **kw):
    return Dispatch(
        call_type=CallType.ASSESS,
        payload=AssessDispatchPayload(question_id=qid, **kw),
    )


async def test_create_and_fetch_sequence(tmp_db, question_page):
    """create_call_sequence + get_sequences_for_call roundtrip."""
    parent = await tmp_db.create_call(
        CallType.PRIORITIZATION,
        scope_page_id=question_page.id,
    )

    seq0 = await tmp_db.create_call_sequence(
        parent_call_id=parent.id,
        scope_question_id=question_page.id,
        position_in_batch=0,
    )
    seq1 = await tmp_db.create_call_sequence(
        parent_call_id=parent.id,
        scope_question_id=question_page.id,
        position_in_batch=1,
    )

    fetched = await tmp_db.get_sequences_for_call(parent.id)
    assert len(fetched) == 2
    assert fetched[0].id == seq0.id
    assert fetched[1].id == seq1.id
    assert fetched[0].position_in_batch == 0
    assert fetched[1].position_in_batch == 1


async def test_calls_linked_to_sequence(tmp_db, question_page):
    """Calls created with sequence_id/sequence_position are retrievable via get_calls_for_sequence."""
    seq = await tmp_db.create_call_sequence(
        parent_call_id=None,
        scope_question_id=question_page.id,
    )

    call_a = await tmp_db.create_call(
        CallType.FIND_CONSIDERATIONS,
        scope_page_id=question_page.id,
        sequence_id=seq.id,
        sequence_position=0,
    )
    call_b = await tmp_db.create_call(
        CallType.ASSESS,
        scope_page_id=question_page.id,
        sequence_id=seq.id,
        sequence_position=1,
    )

    fetched = await tmp_db.get_calls_for_sequence(seq.id)
    assert len(fetched) == 2
    assert fetched[0].id == call_a.id
    assert fetched[1].id == call_b.id
    assert fetched[0].sequence_position == 0
    assert fetched[1].sequence_position == 1


async def test_sequence_fields_roundtrip_on_call(tmp_db, question_page):
    """sequence_id and sequence_position persist through save_call / get_call."""
    seq = await tmp_db.create_call_sequence(
        parent_call_id=None,
        scope_question_id=question_page.id,
    )

    call = await tmp_db.create_call(
        CallType.ASSESS,
        scope_page_id=question_page.id,
        sequence_id=seq.id,
        sequence_position=3,
    )

    reloaded = await tmp_db.get_call(call.id)
    assert reloaded is not None
    assert reloaded.sequence_id == seq.id
    assert reloaded.sequence_position == 3


async def test_no_sequences_returns_empty(tmp_db, question_page):
    """get_sequences_for_call returns empty list when none exist."""
    parent = await tmp_db.create_call(
        CallType.PRIORITIZATION,
        scope_page_id=question_page.id,
    )
    assert await tmp_db.get_sequences_for_call(parent.id) == []


async def test_delete_run_data_cleans_sequences(tmp_db, question_page):
    """delete_run_data removes call_sequences rows."""
    seq = await tmp_db.create_call_sequence(
        parent_call_id=None,
        scope_question_id=question_page.id,
    )
    await tmp_db.create_call(
        CallType.ASSESS,
        scope_page_id=question_page.id,
        sequence_id=seq.id,
        sequence_position=0,
    )

    await tmp_db.delete_run_data()

    rows = (
        await tmp_db.client.table("call_sequences")
        .select("id")
        .eq("run_id", tmp_db.run_id)
        .execute()
    )
    assert len(rows.data) == 0


@pytest.mark.integration
async def test_single_element_sequences_skip_sequence_creation(tmp_db, question_page):
    """Single-element dispatch sequences should NOT create CallSequence records."""
    p_call = await tmp_db.create_call(
        CallType.PRIORITIZATION,
        scope_page_id=question_page.id,
    )
    CallTrace(p_call.id, tmp_db)

    orch = ScriptedOrchestrator(
        tmp_db,
        sequences=[
            [_scout(question_page.id)],
            [_assess(question_page.id)],
        ],
        call_id=p_call.id,
    )
    await orch.run(question_page.id)

    db_seqs = await tmp_db.get_sequences_for_call(p_call.id)
    assert len(db_seqs) == 0

    child_calls = await tmp_db.get_child_calls(p_call.id)
    for c in child_calls:
        assert c.sequence_id is None
        assert c.sequence_position is None


@pytest.mark.integration
async def test_multi_step_sequence_creates_record_and_assigns_positions(tmp_db, question_page):
    """A sequence with [scout, assess] should create one CallSequence and
    assign sequence_position 0 and 1 to the child calls."""
    p_call = await tmp_db.create_call(
        CallType.PRIORITIZATION,
        scope_page_id=question_page.id,
    )
    CallTrace(p_call.id, tmp_db)

    orch = ScriptedOrchestrator(
        tmp_db,
        sequences=[
            [_scout(question_page.id), _assess(question_page.id)],
        ],
        call_id=p_call.id,
    )
    await orch.run(question_page.id)

    db_seqs = await tmp_db.get_sequences_for_call(p_call.id)
    assert len(db_seqs) == 1
    seq_calls = await tmp_db.get_calls_for_sequence(db_seqs[0].id)
    # assess_question creates 2 calls (summarize + assess), so 3 total
    assert len(seq_calls) == 3
    assert seq_calls[0].sequence_position == 0
    assert seq_calls[1].sequence_position == 1
    assert seq_calls[2].sequence_position == 2
    types = [c.call_type for c in seq_calls]
    assert CallType.FIND_CONSIDERATIONS in types
    assert CallType.SUMMARIZE in types
    assert CallType.ASSESS in types


@pytest.mark.integration
async def test_mixed_single_and_multi_step_sequences(tmp_db, question_page):
    """Only multi-step sequences create CallSequence records; single-step ones don't."""
    p_call = await tmp_db.create_call(
        CallType.PRIORITIZATION,
        scope_page_id=question_page.id,
    )
    CallTrace(p_call.id, tmp_db)

    orch = ScriptedOrchestrator(
        tmp_db,
        sequences=[
            [_scout(question_page.id)],
            [_scout(question_page.id), _assess(question_page.id)],
        ],
        call_id=p_call.id,
    )
    await orch.run(question_page.id)

    db_seqs = await tmp_db.get_sequences_for_call(p_call.id)
    assert len(db_seqs) == 1
    assert db_seqs[0].position_in_batch == 1

    seq_calls = await tmp_db.get_calls_for_sequence(db_seqs[0].id)
    # assess_question creates 2 calls (summarize + assess), so 3 total
    assert len(seq_calls) == 3
