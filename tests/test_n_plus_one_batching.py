"""Regression tests for DB N+1 query patterns.

Covers two DB methods that previously issued per-element queries in a loop:

- ``DB.get_stale_dependencies()`` — looked up every ``depends_on`` link's
  target page and change magnitude individually.
- ``DB.resolve_supersession_chain()`` (singular) — walked the supersession
  chain one page at a time.

Each group has behavioural tests (including cyclic-graph termination)
and a query-count test that spies on ``DB._execute`` to assert the round
trip count does not scale with the problem size.

Written against the pre-batching implementation: the behavioural tests
pass on current main, the query-count tests fail, and both pass after
the batching fix.
"""

import pytest_asyncio

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
        credence=6,
        robustness=3,
    )


async def _depends_on(db: DB, dependent: Page, dependency: Page) -> PageLink:
    link = PageLink(
        from_page_id=dependent.id,
        to_page_id=dependency.id,
        link_type=LinkType.DEPENDS_ON,
        direction=ConsiderationDirection.NEUTRAL,
        strength=3.0,
        reasoning="test dependency",
        role=LinkRole.DIRECT,
    )
    await db.save_link(link)
    return link


@pytest_asyncio.fixture
async def claims_abcdef(tmp_db):
    """Six claims saved in the db, returned in order."""
    claims = [_claim(f"Claim {letter}") for letter in "ABCDEF"]
    for c in claims:
        await tmp_db.save_page(c)
    return claims


# ---------------------------------------------------------------------------
# get_stale_dependencies
#
# Note: get_stale_dependencies does not filter by project_id. The
# page_links table has no project_id column, and other link queries in
# database.py scope via keyed page IDs rather than project. These tests
# therefore assert only on the subset of results whose links we created
# ourselves — they are robust to leftover cross-project rows in the
# shared local DB.
# ---------------------------------------------------------------------------


def _subset_by_link_ids(
    stale: list[tuple[PageLink, int | None]],
    link_ids: set[str],
) -> dict[str, int | None]:
    """Return {link_id: magnitude} for only the links we care about."""
    return {link.id: magnitude for link, magnitude in stale if link.id in link_ids}


async def test_stale_deps_does_not_return_active_targets(tmp_db, claims_abcdef):
    """When every DEPENDS_ON target we created is active, none of our
    links should appear in the stale result."""
    a, b, c, d, *_ = claims_abcdef
    link_ab = await _depends_on(tmp_db, a, b)
    link_cd = await _depends_on(tmp_db, c, d)
    our_link_ids = {link_ab.id, link_cd.id}

    stale = await tmp_db.get_stale_dependencies()
    assert _subset_by_link_ids(stale, our_link_ids) == {}


async def test_stale_deps_mix_of_active_and_stale(tmp_db, claims_abcdef):
    """Only the links whose target is superseded should be returned,
    with the change magnitude forwarded from the supersession event."""
    a, b, c, d, e, f = claims_abcdef
    link_ab = await _depends_on(tmp_db, a, b)  # target active -> not stale
    link_cd = await _depends_on(tmp_db, c, d)  # will supersede d with magnitude
    link_ef = await _depends_on(tmp_db, e, f)  # will supersede f without magnitude
    our_link_ids = {link_ab.id, link_cd.id, link_ef.id}

    replacement_d = _claim("Claim D prime")
    replacement_f = _claim("Claim F prime")
    await tmp_db.save_page(replacement_d)
    await tmp_db.save_page(replacement_f)
    await tmp_db.supersede_page(d.id, replacement_d.id, change_magnitude=4)
    await tmp_db.supersede_page(f.id, replacement_f.id)  # no magnitude

    stale = await tmp_db.get_stale_dependencies()
    ours = _subset_by_link_ids(stale, our_link_ids)

    assert ours == {
        link_cd.id: 4,
        link_ef.id: None,
    }


async def test_stale_deps_cyclic_depends_on_graph_terminates(
    tmp_db,
    claims_abcdef,
):
    """A cycle in DEPENDS_ON links must not cause infinite traversal.

    get_stale_dependencies looks at each link's target once, so a cycle
    should simply produce each link once with its target-state result
    and never recurse. This pins the guarantee so nobody accidentally
    introduces transitive walking without cycle detection.
    """
    a, b, c, *_ = claims_abcdef
    # A -> B, B -> C, C -> A (a cycle among three claims).
    link_ab = await _depends_on(tmp_db, a, b)
    link_bc = await _depends_on(tmp_db, b, c)
    link_ca = await _depends_on(tmp_db, c, a)
    our_link_ids = {link_ab.id, link_bc.id, link_ca.id}

    # Supersede B so exactly one of the cycle links becomes stale.
    b_prime = _claim("Claim B prime")
    await tmp_db.save_page(b_prime)
    await tmp_db.supersede_page(b.id, b_prime.id, change_magnitude=2)

    stale = await tmp_db.get_stale_dependencies()
    ours = _subset_by_link_ids(stale, our_link_ids)

    # Only A->B should appear (the link whose target was superseded).
    # C->A and B->C have active targets.
    assert ours == {link_ab.id: 2}


async def test_stale_deps_self_loop_terminates(tmp_db, claims_abcdef):
    """A self-loop (page depends on itself) must not deadlock or recurse."""
    a, *_ = claims_abcdef
    link_aa = await _depends_on(tmp_db, a, a)
    our_link_ids = {link_aa.id}

    # While A is active, the self-loop is not stale.
    initial = await tmp_db.get_stale_dependencies()
    assert _subset_by_link_ids(initial, our_link_ids) == {}

    # Supersede A. The link still points at the old A id, which is now
    # superseded, so the self-loop should be returned exactly once.
    a_prime = _claim("Claim A prime")
    await tmp_db.save_page(a_prime)
    await tmp_db.supersede_page(a.id, a_prime.id, change_magnitude=5)

    stale = await tmp_db.get_stale_dependencies()
    ours = _subset_by_link_ids(stale, our_link_ids)
    assert ours == {link_aa.id: 5}


async def test_stale_deps_query_count_is_constant(
    tmp_db,
    claims_abcdef,
    mocker,
):
    """get_stale_dependencies must issue O(1) DB round trips regardless
    of the number of depends_on links in the workspace.

    Before the batching fix this is O(N_links) (one get_page per link,
    plus one magnitude query per stale link). After the fix it is a
    small constant number of queries.

    Robust against cross-project leftover data: asserts on the query
    count, not on len(stale).
    """
    a, b, c, d, e, f = claims_abcdef
    # 4 depends_on links. B, D, and F are all superseded below, so:
    #   - A->B is stale (target B superseded)
    #   - C->D is stale (target D superseded)
    #   - E->F is stale (target F superseded)
    #   - B->D is stale (target D superseded)
    # All 4 links become stale.
    link_ab = await _depends_on(tmp_db, a, b)
    link_cd = await _depends_on(tmp_db, c, d)
    link_ef = await _depends_on(tmp_db, e, f)
    link_bd = await _depends_on(tmp_db, b, d)
    our_link_ids = {link_ab.id, link_cd.id, link_ef.id, link_bd.id}

    for target in (b, d, f):
        replacement = _claim(f"{target.headline} prime")
        await tmp_db.save_page(replacement)
        await tmp_db.supersede_page(target.id, replacement.id, change_magnitude=3)

    spy = mocker.spy(DB, "_execute")
    start = spy.call_count

    stale = await tmp_db.get_stale_dependencies()

    # Verify all four of our links are in the result (ignoring any
    # leftover cross-project links in the shared local DB).
    ours = _subset_by_link_ids(stale, our_link_ids)
    assert set(ours.keys()) == our_link_ids
    assert all(m == 3 for m in ours.values())

    queries = spy.call_count - start
    # Post-fix target: ~1 (list stale links) + 1 (batched pages) + 1
    # (batched magnitudes) = ~3. Allow modest slack for staged-events
    # bookkeeping but insist it is well under O(N_links).
    #
    # Pre-fix with our 4 test links + any cross-project links the DB
    # might have, this blows well past 6.
    assert queries <= 6, (
        f"expected O(1) queries from get_stale_dependencies, got {queries}"
    )


# ---------------------------------------------------------------------------
# resolve_supersession_chain (singular)
#
# A single chain is fundamentally O(depth) round trips — there is no
# way to do better without a recursive-CTE RPC. The real N+1 for this
# API lives at *call sites* that loop over N pages calling the singular
# N times; see test_context_builders_refresh_uses_batched_resolve in
# test_big_assess_context.py for the call-site regression test.
#
# The tests here only pin the safety property (cyclic chains terminate).
# ---------------------------------------------------------------------------


async def test_resolve_chain_cycle_terminates(tmp_db):
    """A supersession cycle (A superseded_by B, B superseded_by A) must
    return None rather than hanging."""
    a = _claim("Cycle A")
    b = _claim("Cycle B")
    await tmp_db.save_page(a)
    await tmp_db.save_page(b)
    await tmp_db.supersede_page(a.id, b.id)
    # Reach under the API to create a cycle: B superseded_by A. We can't
    # use supersede_page because B was already marked superseded above.
    await tmp_db._execute(
        tmp_db.client.table("pages")
        .update({"is_superseded": True, "superseded_by": a.id})
        .eq("id", b.id)
    )

    # A -> B -> A should terminate with None rather than hanging.
    result = await tmp_db.resolve_supersession_chain(a.id, max_depth=10)
    assert result is None

    result = await tmp_db.resolve_supersession_chain(b.id, max_depth=10)
    assert result is None
