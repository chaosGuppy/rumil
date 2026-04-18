"""Tests for the stats dashboard RPCs (compute_project_stats, compute_question_stats).

These hit the real local Supabase via the tmp_db fixture. Each test builds a
small graph of pages/links/calls, invokes the RPC through the thin DB wrapper,
and asserts on observable properties of the returned blob.
"""

import pytest
import pytest_asyncio

from rumil.models import (
    Call,
    CallStatus,
    CallType,
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)


async def _make_page(
    tmp_db,
    *,
    page_type: PageType,
    headline: str = "x",
    credence: int | None = None,
    robustness: int | None = None,
) -> Page:
    page = Page(
        page_type=page_type,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=headline,
        headline=headline,
        credence=credence,
        robustness=robustness,
    )
    await tmp_db.save_page(page)
    return page


async def _link(tmp_db, src: Page, dst: Page, link_type: LinkType) -> PageLink:
    link = PageLink(from_page_id=src.id, to_page_id=dst.id, link_type=link_type)
    await tmp_db.save_link(link)
    return link


async def _call(tmp_db, scope: Page, call_type: CallType = CallType.FIND_CONSIDERATIONS) -> Call:
    call = Call(
        call_type=call_type,
        workspace=Workspace.RESEARCH,
        scope_page_id=scope.id,
        status=CallStatus.COMPLETE,
    )
    await tmp_db.save_call(call)
    return call


@pytest_asyncio.fixture
async def small_project(tmp_db):
    """Build a small hand-countable project graph.

    Shape:
        question_a -- child_question --> question_b
        claim_1 -- consideration --> question_a  (direction: supports)
        claim_2 -- consideration --> question_a  (direction: opposes)
        judgement_1 -- answers --> question_a

    Plus 2 find_considerations calls and 1 assess call against question_a,
    and 1 find_considerations call against question_b.
    """
    q_a = await _make_page(tmp_db, page_type=PageType.QUESTION, headline="q_a")
    q_b = await _make_page(tmp_db, page_type=PageType.QUESTION, headline="q_b")
    c_1 = await _make_page(
        tmp_db, page_type=PageType.CLAIM, headline="c_1", credence=7, robustness=3
    )
    c_2 = await _make_page(
        tmp_db, page_type=PageType.CLAIM, headline="c_2", credence=3, robustness=2
    )
    j_1 = await _make_page(tmp_db, page_type=PageType.JUDGEMENT, headline="j_1", robustness=4)

    await _link(tmp_db, q_a, q_b, LinkType.CHILD_QUESTION)
    await _link(tmp_db, c_1, q_a, LinkType.CONSIDERATION)
    await _link(tmp_db, c_2, q_a, LinkType.CONSIDERATION)
    await _link(tmp_db, j_1, q_a, LinkType.ANSWERS)

    await _call(tmp_db, q_a, CallType.FIND_CONSIDERATIONS)
    await _call(tmp_db, q_a, CallType.FIND_CONSIDERATIONS)
    await _call(tmp_db, q_a, CallType.ASSESS)
    await _call(tmp_db, q_b, CallType.FIND_CONSIDERATIONS)

    return {
        "q_a": q_a,
        "q_b": q_b,
        "c_1": c_1,
        "c_2": c_2,
        "j_1": j_1,
    }


@pytest.mark.asyncio
async def test_project_stats_totals(tmp_db, small_project):
    blob = await tmp_db.get_project_stats(tmp_db.project_id)

    assert blob["pages_total"] == 5
    assert blob["pages_by_type"] == {"question": 2, "claim": 2, "judgement": 1}
    assert blob["links_total"] == 4
    assert blob["links_by_type"] == {
        "child_question": 1,
        "consideration": 2,
        "answers": 1,
    }


@pytest.mark.asyncio
async def test_project_stats_degree_matrix(tmp_db, small_project):
    blob = await tmp_db.get_project_stats(tmp_db.project_id)
    matrix = blob["degree_matrix"]

    # Both claims emit one consideration link, so avg_out for claim/consideration = 1.0
    claim_cell = matrix["claim"]["consideration"]
    assert claim_cell["avg_out"] == pytest.approx(1.0)
    assert claim_cell["avg_in"] == pytest.approx(0.0)

    # Questions receive 2 considerations across 2 question pages = avg_in 1.0
    question_cell = matrix["question"]["consideration"]
    assert question_cell["avg_out"] == pytest.approx(0.0)
    assert question_cell["avg_in"] == pytest.approx(1.0)

    # 1 child_question link from q_a to q_b, 2 question pages:
    # avg_out 0.5, avg_in 0.5
    child_cell = matrix["question"]["child_question"]
    assert child_cell["avg_out"] == pytest.approx(0.5)
    assert child_cell["avg_in"] == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_project_stats_histograms(tmp_db, small_project):
    blob = await tmp_db.get_project_stats(tmp_db.project_id)

    # Keys are stringified ints (JSONB keys must be text)
    credence = blob["credence_histogram"]
    robustness = blob["robustness_histogram"]

    # Credence is claim-only. Claims: 7, 3.
    assert credence.get("7") == 1
    assert credence.get("3") == 1
    assert credence.get("5") is None

    # Claims: 3, 2. Judgement: 4.
    assert robustness.get("3") == 1
    assert robustness.get("2") == 1
    assert robustness.get("4") == 1


@pytest.mark.asyncio
async def test_project_stats_calls_per_question(tmp_db, small_project):
    blob = await tmp_db.get_project_stats(tmp_db.project_id)
    by_qid = {entry["question_id"]: entry for entry in blob["calls_per_question"]}

    assert small_project["q_a"].id in by_qid
    assert small_project["q_b"].id in by_qid

    q_a_entry = by_qid[small_project["q_a"].id]
    assert q_a_entry["total"] == 3
    assert q_a_entry["by_type"] == {"find_considerations": 2, "assess": 1}
    # q_a has 1 child question (q_b), 2 considerations (c_1, c_2), 1 judgement (j_1)
    assert q_a_entry["child_questions"] == 1
    assert q_a_entry["considerations"] == 2
    assert q_a_entry["judgements"] == 1

    q_b_entry = by_qid[small_project["q_b"].id]
    assert q_b_entry["total"] == 1
    assert q_b_entry["by_type"] == {"find_considerations": 1}
    assert q_b_entry["child_questions"] == 0
    assert q_b_entry["considerations"] == 0
    assert q_b_entry["judgements"] == 0


@pytest.mark.asyncio
async def test_project_stats_superseded_excluded(tmp_db, small_project):
    """Superseding a claim should drop it from totals, histograms, and the link matrix."""
    # Supersede c_1 directly via the pages table so the aggregate reflects it.
    await tmp_db._execute(
        tmp_db.client.table("pages")
        .update({"is_superseded": True})
        .eq("id", small_project["c_1"].id)
    )

    blob = await tmp_db.get_project_stats(tmp_db.project_id)

    assert blob["pages_total"] == 4
    assert blob["pages_by_type"]["claim"] == 1
    # One consideration link is now gone because its source is inactive.
    assert blob["links_by_type"]["consideration"] == 1


@pytest.mark.asyncio
async def test_project_stats_empty_project(tmp_db):
    blob = await tmp_db.get_project_stats(tmp_db.project_id)
    assert blob["pages_total"] == 0
    assert blob["links_total"] == 0
    assert blob["pages_by_type"] == {}
    assert blob["links_by_type"] == {}
    assert blob["degree_matrix"] == {}
    assert blob["calls_per_question"] == []


@pytest_asyncio.fixture
async def linear_chain(tmp_db):
    """Build a chain q0 -- child -- q1 -- child -- q2 -- child -- q3 -- child -- q4.

    Used to test the 2-hop boundary: from q0, we should see q0..q2 but not q3/q4.
    """
    pages = []
    for i in range(5):
        p = await _make_page(tmp_db, page_type=PageType.QUESTION, headline=f"q{i}")
        pages.append(p)
    for i in range(4):
        await _link(tmp_db, pages[i], pages[i + 1], LinkType.CHILD_QUESTION)
    return pages


@pytest.mark.asyncio
async def test_question_stats_two_hop_boundary(tmp_db, linear_chain):
    blob = await tmp_db.get_question_stats(linear_chain[0].id)
    assert blob["subgraph_page_count"] == 3
    assert blob["pages_total"] == 3
    # Links within the subgraph: q0->q1, q1->q2 (q2->q3 is out).
    assert blob["links_total"] == 2


@pytest.mark.asyncio
async def test_question_stats_undirected(tmp_db):
    """Parents reached only via an inbound link should still be in the neighborhood."""
    parent = await _make_page(tmp_db, page_type=PageType.QUESTION, headline="parent")
    child = await _make_page(tmp_db, page_type=PageType.QUESTION, headline="child")
    # Link goes parent -> child (so the child is reachable by walking an inbound link)
    await _link(tmp_db, parent, child, LinkType.CHILD_QUESTION)

    blob = await tmp_db.get_question_stats(child.id)
    assert blob["subgraph_page_count"] == 2
    assert blob["pages_by_type"] == {"question": 2}
    assert blob["links_total"] == 1


@pytest.mark.asyncio
async def test_question_stats_isolated(tmp_db):
    """A question with no links should have subgraph = just itself."""
    q = await _make_page(tmp_db, page_type=PageType.QUESTION, headline="lonely")
    blob = await tmp_db.get_question_stats(q.id)
    assert blob["subgraph_page_count"] == 1
    assert blob["pages_by_type"] == {"question": 1}
    assert blob["links_total"] == 0
    assert blob["calls_per_question"] == [
        {
            "question_id": q.id,
            "headline": "lonely",
            "by_type": {},
            "total": 0,
            "child_questions": 0,
            "considerations": 0,
            "judgements": 0,
        }
    ]


@pytest.mark.asyncio
async def test_question_stats_nonexistent(tmp_db):
    blob = await tmp_db.get_question_stats("nonexistent-id-xyz")
    assert blob["subgraph_page_count"] == 0
    assert blob["pages_total"] == 0
    assert blob["links_total"] == 0


@pytest.mark.asyncio
async def test_question_stats_subgraph_depths(tmp_db, linear_chain):
    """Each node in the returned subgraph should report its min hop distance
    from the anchor. On a chain q0–q1–q2–q3–q4, scoping to q0 gives depths
    0, 1, 2 for q0..q2 respectively."""
    blob = await tmp_db.get_question_stats(linear_chain[0].id)
    nodes = blob["subgraph"]["nodes"]
    depth_by_id = {n["id"]: n["depth"] for n in nodes}

    assert depth_by_id[linear_chain[0].id] == 0
    assert depth_by_id[linear_chain[1].id] == 1
    assert depth_by_id[linear_chain[2].id] == 2
    assert linear_chain[3].id not in depth_by_id
    assert linear_chain[4].id not in depth_by_id


@pytest.mark.asyncio
async def test_question_stats_subgraph_edges(tmp_db, small_project):
    """Edges returned for a subgraph should match the active links between
    subgraph pages, carrying link_type intact."""
    blob = await tmp_db.get_question_stats(small_project["q_a"].id)
    edges = blob["subgraph"]["edges"]
    edge_set = {(e["from_page_id"], e["to_page_id"], e["link_type"]) for e in edges}

    q_a_id = small_project["q_a"].id
    assert (small_project["c_1"].id, q_a_id, "consideration") in edge_set
    assert (small_project["c_2"].id, q_a_id, "consideration") in edge_set
    assert (small_project["j_1"].id, q_a_id, "answers") in edge_set
    assert (q_a_id, small_project["q_b"].id, "child_question") in edge_set


@pytest.mark.asyncio
async def test_question_stats_subgraph_isolated(tmp_db):
    """A question with no neighbors has a subgraph containing only itself."""
    q = await _make_page(tmp_db, page_type=PageType.QUESTION, headline="alone")
    blob = await tmp_db.get_question_stats(q.id)
    subgraph = blob["subgraph"]

    assert len(subgraph["nodes"]) == 1
    assert subgraph["nodes"][0]["id"] == q.id
    assert subgraph["nodes"][0]["depth"] == 0
    assert subgraph["edges"] == []
