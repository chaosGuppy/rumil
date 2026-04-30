"""Test that execute_with_source_creation handles scrape failures correctly."""

from rumil.models import (
    Call,
    CallStatus,
    CallType,
    Workspace,
)
from rumil.moves.create_claim import execute_with_source_creation


async def test_create_claim_errors_when_source_url_unscrapable(
    tmp_db,
    question_page,
    mocker,
):
    call = Call(
        call_type=CallType.WEB_RESEARCH,
        workspace=Workspace.RESEARCH,
        scope_page_id=question_page.id,
        status=CallStatus.PENDING,
    )
    await tmp_db.save_call(call)

    mocker.patch(
        "rumil.moves.create_claim.scrape_url",
        return_value=None,
    )

    result, payload = await execute_with_source_creation(
        {
            "headline": "Some claim",
            "content": "Claim content",
            "source_urls": ["https://unreachable.example.com/article"],
        },
        call,
        tmp_db,
        source_cache={},
    )

    assert payload is None
    assert "unreachable.example.com" in result.message
    assert "different" in result.message.lower()
    assert result.created_page_id == ""


async def test_create_claim_succeeds_when_no_source_urls(
    tmp_db,
    question_page,
):
    call = Call(
        call_type=CallType.WEB_RESEARCH,
        workspace=Workspace.RESEARCH,
        scope_page_id=question_page.id,
        status=CallStatus.PENDING,
    )
    await tmp_db.save_call(call)

    result, payload = await execute_with_source_creation(
        {
            "headline": "Some claim",
            "content": "Claim content",
            "credence": 5,
            "credence_reasoning": "Placeholder reasoning.",
            "robustness": 2,
            "robustness_reasoning": "Placeholder robustness reasoning.",
        },
        call,
        tmp_db,
        source_cache={},
    )

    assert payload is not None
    assert result.created_page_id != ""
    assert "ERROR" not in result.message
