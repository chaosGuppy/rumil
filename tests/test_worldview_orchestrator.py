import pytest
import pytest_asyncio

from rumil.models import (
    Call,
    CallStatus,
    CallType,
    ConsiderationDirection,
    LinkRole,
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Suggestion,
    SuggestionType,
    Workspace,
)
from rumil.orchestrators.worldview import WorldviewOrchestrator


@pytest_asyncio.fixture
async def question(tmp_db):
    page = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="What drives deforestation in the Amazon?",
        headline="What drives deforestation in the Amazon?",
    )
    await tmp_db.save_page(page)
    return page


async def _add_considerations(tmp_db, question_id: str, n: int) -> list[Page]:
    claims = []
    for i in range(n):
        claim = Page(
            page_type=PageType.CLAIM,
            layer=PageLayer.SQUIDGY,
            workspace=Workspace.RESEARCH,
            content=f"Test consideration {i}",
            headline=f"Consideration {i}",
        )
        await tmp_db.save_page(claim)
        await tmp_db.save_link(
            PageLink(
                from_page_id=claim.id,
                to_page_id=question_id,
                link_type=LinkType.CONSIDERATION,
                strength=3.0,
                direction=ConsiderationDirection.SUPPORTS,
                role=LinkRole.DIRECT,
            )
        )
        claims.append(claim)
    return claims


async def _add_judgement(tmp_db, question_id: str) -> Page:
    judgement = Page(
        page_type=PageType.JUDGEMENT,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Test judgement",
        headline="Test judgement",
    )
    await tmp_db.save_page(judgement)
    await tmp_db.save_link(
        PageLink(
            from_page_id=judgement.id,
            to_page_id=question_id,
            link_type=LinkType.ANSWERS,
        )
    )
    return judgement


async def _add_completed_call(
    tmp_db, question_id: str, call_type: CallType,
) -> Call:
    call = Call(
        call_type=call_type,
        workspace=Workspace.RESEARCH,
        scope_page_id=question_id,
        status=CallStatus.COMPLETE,
    )
    await tmp_db.save_call(call)
    return call


async def test_few_pages_returns_explore(tmp_db, question):
    await _add_considerations(tmp_db, question.id, 2)

    orch = WorldviewOrchestrator(tmp_db)
    mode = await orch._decide_mode(question.id)
    assert mode == "explore"


async def test_pending_cascade_suggestion_triggers_evaluate(tmp_db, question):
    claims = await _add_considerations(tmp_db, question.id, 4)

    suggestion = Suggestion(
        project_id=tmp_db.project_id,
        suggestion_type=SuggestionType.CASCADE_REVIEW,
        target_page_id=claims[0].id,
        source_page_id=claims[1].id,
        payload={"reasoning": "upstream claim updated"},
    )
    await tmp_db.save_suggestion(suggestion)

    orch = WorldviewOrchestrator(tmp_db)
    mode = await orch._decide_mode(question.id)
    assert mode == "evaluate"


async def test_no_judgement_with_five_considerations_triggers_evaluate(tmp_db, question):
    await _add_considerations(tmp_db, question.id, 5)

    orch = WorldviewOrchestrator(tmp_db)
    mode = await orch._decide_mode(question.id)
    assert mode == "evaluate"


async def test_more_explores_than_assesses_triggers_evaluate(tmp_db, question):
    await _add_considerations(tmp_db, question.id, 3)
    await _add_judgement(tmp_db, question.id)

    await _add_completed_call(tmp_db, question.id, CallType.FIND_CONSIDERATIONS)
    await _add_completed_call(tmp_db, question.id, CallType.FIND_CONSIDERATIONS)

    orch = WorldviewOrchestrator(tmp_db)
    mode = await orch._decide_mode(question.id)
    assert mode == "evaluate"


async def test_default_is_explore(tmp_db, question):
    await _add_considerations(tmp_db, question.id, 3)
    await _add_judgement(tmp_db, question.id)

    await _add_completed_call(tmp_db, question.id, CallType.FIND_CONSIDERATIONS)
    await _add_completed_call(tmp_db, question.id, CallType.ASSESS)

    orch = WorldviewOrchestrator(tmp_db)
    mode = await orch._decide_mode(question.id)
    assert mode == "explore"
