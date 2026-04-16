"""Tests for the staleness signal surfaced to prioritization.

_build_staleness_signal reads stale dependency links (DEPENDS_ON targets
that have been superseded) and formats them as context text for the
prioritizer LLM. build_prioritization_context should include this section
when stale deps exist and omit it when they don't.

Written against pre-implementation main: the tests that assert the
staleness section is present in the output must fail until the feature
is added.
"""

import pytest_asyncio

from rumil.context import build_prioritization_context
from rumil.database import DB
from rumil.models import (
    ConsiderationDirection,
    LinkRole,
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)


def _claim(headline: str) -> Page:
    return Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=f"Content of {headline}",
        headline=headline,
        credence=7,
        robustness=3,
    )


def _question(headline: str = "Root question") -> Page:
    return Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=headline,
        headline=headline,
    )


async def _depends_on(db: DB, dependent: Page, dependency: Page) -> PageLink:
    link = PageLink(
        from_page_id=dependent.id,
        to_page_id=dependency.id,
        link_type=LinkType.DEPENDS_ON,
        direction=ConsiderationDirection.NEUTRAL,
        strength=4.0,
        reasoning="test dependency",
        role=LinkRole.DIRECT,
    )
    await db.save_link(link)
    return link


async def _consideration(db: DB, claim: Page, question: Page) -> PageLink:
    link = PageLink(
        from_page_id=claim.id,
        to_page_id=question.id,
        link_type=LinkType.CONSIDERATION,
        direction=ConsiderationDirection.SUPPORTS,
        strength=3.0,
        reasoning="test consideration",
    )
    await db.save_link(link)
    return link


@pytest_asyncio.fixture
async def question_with_stale_dep(tmp_db):
    """A question with a claim that depends on a superseded page.

    Structure:
      root_q <-- claim_a (consideration, SUPPORTS)
      claim_a --depends_on--> claim_b [SUPERSEDED by claim_b_prime]

    Returns (question, claim_a, claim_b, claim_b_prime, link_dep).
    """
    q = _question("What is the right policy?")
    claim_a = _claim("Claim A: policy X is optimal")
    claim_b = _claim("Claim B: cost estimate is $1M")
    claim_b_prime = _claim("Claim B prime: cost estimate is $5M")

    await tmp_db.save_page(q)
    await tmp_db.save_page(claim_a)
    await tmp_db.save_page(claim_b)
    await tmp_db.save_page(claim_b_prime)

    await _consideration(tmp_db, claim_a, q)
    link_dep = await _depends_on(tmp_db, claim_a, claim_b)
    await tmp_db.supersede_page(claim_b.id, claim_b_prime.id, change_magnitude=4)

    return q, claim_a, claim_b, claim_b_prime, link_dep


async def test_staleness_signal_absent_when_no_stale_deps(tmp_db):
    """When no DEPENDS_ON links exist (or all targets are active),
    the staleness section should not appear in the prioritization context."""
    q = _question()
    await tmp_db.save_page(q)

    ctx_text, _ = await build_prioritization_context(tmp_db, q.id)
    assert "Stale Dependencies" not in ctx_text


async def test_staleness_signal_absent_when_deps_active(tmp_db):
    """Active dependencies should not trigger a staleness section."""
    q = _question()
    claim_a = _claim("Claim A")
    claim_b = _claim("Claim B")
    await tmp_db.save_page(q)
    await tmp_db.save_page(claim_a)
    await tmp_db.save_page(claim_b)

    await _consideration(tmp_db, claim_a, q)
    await _depends_on(tmp_db, claim_a, claim_b)

    ctx_text, _ = await build_prioritization_context(tmp_db, q.id)
    assert "Stale Dependencies" not in ctx_text


async def test_staleness_signal_present_when_deps_superseded(
    tmp_db,
    question_with_stale_dep,
):
    """When a DEPENDS_ON target is superseded, the staleness section
    should appear in the prioritization context with enough detail for
    the prioritizer to decide whether reassessment is worth budget."""
    q, claim_a, claim_b, claim_b_prime, _link = question_with_stale_dep

    ctx_text, _ = await build_prioritization_context(tmp_db, q.id)

    assert "Stale Dependencies" in ctx_text
    # The dependent's headline should appear.
    assert claim_a.headline in ctx_text
    # The superseded target's headline should appear.
    assert claim_b.headline in ctx_text
    # The change magnitude should appear.
    assert "4" in ctx_text


async def test_staleness_signal_shows_link_strength(
    tmp_db,
    question_with_stale_dep,
):
    """The staleness section should include the link strength so the
    prioritizer can weigh how critical the dependency is."""
    q, *_ = question_with_stale_dep
    ctx_text, _ = await build_prioritization_context(tmp_db, q.id)

    # The link was created with strength 4.0.
    assert "strength" in ctx_text.lower() or "4" in ctx_text


async def test_staleness_signal_handles_missing_magnitude(tmp_db):
    """When a supersession event has no change_magnitude, the section
    should still render without crashing."""
    q = _question()
    claim_a = _claim("Claim A")
    claim_b = _claim("Claim B")
    claim_b2 = _claim("Claim B replacement")
    await tmp_db.save_page(q)
    await tmp_db.save_page(claim_a)
    await tmp_db.save_page(claim_b)
    await tmp_db.save_page(claim_b2)

    await _consideration(tmp_db, claim_a, q)
    await _depends_on(tmp_db, claim_a, claim_b)
    await tmp_db.supersede_page(claim_b.id, claim_b2.id)  # no magnitude

    ctx_text, _ = await build_prioritization_context(tmp_db, q.id)
    assert "Stale Dependencies" in ctx_text
    assert claim_a.headline in ctx_text
    assert claim_b.headline in ctx_text
