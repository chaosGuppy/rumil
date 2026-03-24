"""Tests for judgement superseding behaviour.

Tests 1-4 are written against the DESIRED behaviour and will FAIL on the
current (buggy) code where create_judgement uses LinkType.CONSIDERATION
instead of LinkType.RELATED. Tests 5-7 cover already-working paths.
"""

from rumil.models import LinkType, MoveType, Page, PageLayer, PageLink, PageType, Workspace
from rumil.moves import MOVES
from rumil.moves.base import MoveState


def _judgement_payload(question_page, **overrides):
    base = {
        "headline": "The sky is blue",
        "content": "Based on evidence, the sky is blue.",
        "links": [{
            "question_id": question_page.id[:8],
            "strength": 3.5,
            "reasoning": "Bears on question",
        }],
    }
    base.update(overrides)
    return base


async def _create_linked_judgement(tmp_db, scout_call, question_page, headline="Judgement"):
    """Create a judgement with an inline link to question_page, return its ID."""
    state = MoveState(scout_call, tmp_db)
    tool = MOVES[MoveType.CREATE_JUDGEMENT].bind(state)
    await tool.fn(_judgement_payload(question_page, headline=headline))
    assert len(state.created_page_ids) == 1
    return state.created_page_ids[0]


async def test_create_judgement_inline_link_is_related(tmp_db, scout_call, question_page):
    """Inline judgement links should use RELATED, not CONSIDERATION."""
    jid = await _create_linked_judgement(tmp_db, scout_call, question_page)

    links = await tmp_db.get_links_from(jid)
    related_links = [l for l in links if l.to_page_id == question_page.id]
    assert len(related_links) == 1
    assert related_links[0].link_type == LinkType.RELATED


async def test_create_judgement_inline_link_found_by_get_judgements(
    tmp_db, scout_call, question_page,
):
    """A judgement created with inline links should be found by get_judgements_for_question."""
    jid = await _create_linked_judgement(tmp_db, scout_call, question_page)

    judgements = await tmp_db.get_judgements_for_question(question_page.id)
    assert any(j.id == jid for j in judgements)


async def test_create_judgement_supersedes_old_judgement(
    tmp_db, scout_call, question_page,
):
    """Creating a second judgement on the same question should supersede the first."""
    j1_id = await _create_linked_judgement(
        tmp_db, scout_call, question_page, headline="First judgement",
    )
    j2_id = await _create_linked_judgement(
        tmp_db, scout_call, question_page, headline="Second judgement",
    )

    j1 = await tmp_db.get_page(j1_id)
    assert j1 is not None
    assert j1.is_superseded is True
    assert j1.superseded_by == j2_id

    active = await tmp_db.get_judgements_for_question(question_page.id)
    active_ids = [j.id for j in active]
    assert j2_id in active_ids
    assert j1_id not in active_ids


async def test_create_judgement_supersedes_multiple_old_judgements(
    tmp_db, scout_call, question_page,
):
    """Creating three judgements sequentially should leave only the last active."""
    j1_id = await _create_linked_judgement(
        tmp_db, scout_call, question_page, headline="First",
    )
    j2_id = await _create_linked_judgement(
        tmp_db, scout_call, question_page, headline="Second",
    )
    j3_id = await _create_linked_judgement(
        tmp_db, scout_call, question_page, headline="Third",
    )

    for old_id in (j1_id, j2_id):
        old = await tmp_db.get_page(old_id)
        assert old is not None
        assert old.is_superseded is True

    active = await tmp_db.get_judgements_for_question(question_page.id)
    assert len(active) == 1
    assert active[0].id == j3_id


async def test_link_related_supersedes_old_judgement(tmp_db, scout_call, question_page):
    """LINK_RELATED move should supersede old judgements on the same question."""
    j1 = Page(
        page_type=PageType.JUDGEMENT,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="First judgement content",
        headline="J1",
    )
    j2 = Page(
        page_type=PageType.JUDGEMENT,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Second judgement content",
        headline="J2",
    )
    await tmp_db.save_page(j1)
    await tmp_db.save_page(j2)

    state1 = MoveState(scout_call, tmp_db)
    tool1 = MOVES[MoveType.LINK_RELATED].bind(state1)
    await tool1.fn({
        "from_page_id": j1.id[:8],
        "to_page_id": question_page.id[:8],
        "reasoning": "Judgement on question",
    })

    state2 = MoveState(scout_call, tmp_db)
    tool2 = MOVES[MoveType.LINK_RELATED].bind(state2)
    await tool2.fn({
        "from_page_id": j2.id[:8],
        "to_page_id": question_page.id[:8],
        "reasoning": "Updated judgement",
    })

    j1_after = await tmp_db.get_page(j1.id)
    assert j1_after is not None
    assert j1_after.is_superseded is True
    assert j1_after.superseded_by == j2.id

    active = await tmp_db.get_judgements_for_question(question_page.id)
    assert len(active) == 1
    assert active[0].id == j2.id


async def test_create_judgement_no_links_no_superseding(tmp_db, scout_call):
    """A judgement created without links should not trigger any superseding."""
    state = MoveState(scout_call, tmp_db)
    tool = MOVES[MoveType.CREATE_JUDGEMENT].bind(state)
    await tool.fn({
        "headline": "Standalone judgement",
        "content": "Not linked to any question.",
    })

    assert len(state.created_page_ids) == 1
    page = await tmp_db.get_page(state.created_page_ids[0])
    assert page is not None
    assert page.page_type is PageType.JUDGEMENT
    assert page.is_superseded is False


async def test_get_judgements_excludes_superseded(tmp_db, question_page):
    """get_judgements_for_question should not return superseded judgements."""
    j1 = Page(
        page_type=PageType.JUDGEMENT,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Will be superseded",
        headline="Old judgement",
    )
    j2 = Page(
        page_type=PageType.JUDGEMENT,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="The replacement",
        headline="New judgement",
    )
    await tmp_db.save_page(j1)
    await tmp_db.save_page(j2)

    await tmp_db.save_link(PageLink(
        from_page_id=j1.id,
        to_page_id=question_page.id,
        link_type=LinkType.RELATED,
    ))
    await tmp_db.save_link(PageLink(
        from_page_id=j2.id,
        to_page_id=question_page.id,
        link_type=LinkType.RELATED,
    ))

    before = await tmp_db.get_judgements_for_question(question_page.id)
    assert len(before) == 2

    await tmp_db.supersede_page(j1.id, j2.id)

    after = await tmp_db.get_judgements_for_question(question_page.id)
    assert len(after) == 1
    assert after[0].id == j2.id
