"""Test that execute_with_source_creation handles scrape failures correctly."""

import time

from rumil.models import (
    Call,
    CallStatus,
    CallType,
    Workspace,
)
from rumil.moves.create_claim import execute_with_source_creation
from rumil.scraper import ScrapedPage


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


async def test_partially_scraped_sources_surface_on_failure(
    tmp_db,
    question_page,
    mocker,
):
    """When some URLs scrape and a later one fails, the successfully-scraped
    sources are still surfaced via extra_created_ids so the wrapper can
    register them in state.created_page_ids. Otherwise they become orphan DB
    rows invisible to the closing review.
    """
    successful = ScrapedPage(
        url="https://good.example.com/page",
        title="Good page",
        content="Some real content.",
        fetched_at=str(time.time()),
    )

    async def fake_scrape(url, **_kwargs):
        return successful if "good" in url else None

    mocker.patch("rumil.moves.create_claim.scrape_url", side_effect=fake_scrape)

    call = Call(
        call_type=CallType.WEB_RESEARCH,
        workspace=Workspace.RESEARCH,
        scope_page_id=question_page.id,
        status=CallStatus.PENDING,
    )
    await tmp_db.save_call(call)

    source_cache: dict[str, str] = {}
    result, payload = await execute_with_source_creation(
        {
            "headline": "Some claim",
            "content": "Claim content",
            "source_urls": [
                "https://good.example.com/page",
                "https://unreachable.example.com/article",
            ],
        },
        call,
        tmp_db,
        source_cache=source_cache,
    )

    assert payload is None
    assert result.created_page_id == ""
    assert "unreachable.example.com" in result.message
    assert result.extra_created_ids
    assert len(result.extra_created_ids) == 1
    assert result.extra_created_ids[0] in source_cache.values()
