"""Tests for the FLAG_ISSUE move and its env-var gating in get_moves_for_call."""

import pytest

from rumil.available_moves import get_moves_for_call
from rumil.models import CallType, MoveType
from rumil.moves.flag_issue import FlagIssuePayload, execute
from rumil.settings import override_settings


async def _read_flags(db, call_id):
    res = await db._execute(
        db.client.table("page_flags").select("*").eq("call_id", call_id)
    )
    return res.data


async def test_flag_issue_persists_row_with_expected_fields(tmp_db, scout_call):
    payload = FlagIssuePayload(
        category="problem",
        message="The preamble doesn't explain what counts as a consideration.",
    )

    result = await execute(payload, scout_call, tmp_db)

    rows = await _read_flags(tmp_db, scout_call.id)
    assert len(rows) == 1
    row = rows[0]
    assert row["flag_type"] == "issue"
    assert row["call_id"] == scout_call.id
    assert row["page_id"] is None
    assert (
        row["note"]
        == "[problem] The preamble doesn't explain what counts as a consideration."
    )
    assert result.message == "Flag recorded. Continue your main task."


async def test_flag_issue_appends_suggested_fix_when_present(tmp_db, scout_call):
    payload = FlagIssuePayload(
        category="improvement",
        message="Context is missing the parent question's judgement.",
        suggested_fix="Include parent judgements in build_embedding_based_context.",
    )

    await execute(payload, scout_call, tmp_db)

    rows = await _read_flags(tmp_db, scout_call.id)
    assert len(rows) == 1
    note = rows[0]["note"]
    assert note.startswith(
        "[improvement] Context is missing the parent question's judgement."
    )
    assert (
        "\n\nSuggested fix: Include parent judgements in build_embedding_based_context."
        in note
    )


@pytest.mark.parametrize("category", ("problem", "improvement"))
async def test_flag_issue_category_is_recorded_in_note(tmp_db, scout_call, category):
    payload = FlagIssuePayload(category=category, message="x")

    await execute(payload, scout_call, tmp_db)

    rows = await _read_flags(tmp_db, scout_call.id)
    assert rows[0]["note"].startswith(f"[{category}]")


def test_flag_issue_not_appended_when_disabled():
    with override_settings(enable_flag_issue=False):
        moves = get_moves_for_call(CallType.FIND_CONSIDERATIONS)
    assert MoveType.FLAG_ISSUE not in moves


def test_flag_issue_appended_when_enabled_and_list_non_empty():
    with override_settings(enable_flag_issue=False):
        baseline = list(get_moves_for_call(CallType.FIND_CONSIDERATIONS))

    with override_settings(enable_flag_issue=True):
        enabled = list(get_moves_for_call(CallType.FIND_CONSIDERATIONS))

    assert MoveType.FLAG_ISSUE not in baseline
    assert enabled == [*baseline, MoveType.FLAG_ISSUE]


def test_flag_issue_not_appended_when_preset_list_is_empty():
    with override_settings(enable_flag_issue=True):
        moves = get_moves_for_call(CallType.PRIORITIZATION)
    assert list(moves) == []


@pytest.mark.parametrize(
    "call_type",
    (
        CallType.FIND_CONSIDERATIONS,
        CallType.ASSESS,
        CallType.INGEST,
        CallType.WEB_RESEARCH,
        CallType.SCOUT_SUBQUESTIONS,
    ),
)
def test_flag_issue_appended_for_every_non_empty_preset(call_type):
    with override_settings(enable_flag_issue=True):
        moves = get_moves_for_call(call_type)
    assert MoveType.FLAG_ISSUE in moves


def test_flag_issue_not_duplicated_if_already_present():
    with override_settings(enable_flag_issue=True):
        moves_a = list(get_moves_for_call(CallType.FIND_CONSIDERATIONS))
        moves_b = list(get_moves_for_call(CallType.FIND_CONSIDERATIONS))
    assert moves_a.count(MoveType.FLAG_ISSUE) == 1
    assert moves_b.count(MoveType.FLAG_ISSUE) == 1
