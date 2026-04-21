"""Tests for the processes package (Investigator / Robustifier / Surveyor).

Coverage:

- Readback assembly: pure DB-behavioral tests that populate rows and
  verify the correct typed delta is reconstructed. No LLM, no process
  internals.
- Delta discriminator: pydantic shape tests.
- Process wrappers: real-LLM end-to-end runs with tiny budgets. Assert
  only on structural outcomes that hold regardless of what the LLM
  returns. These are marked ``llm`` or ``integration`` per the
  repo's conventions.

Mocking ``TwoPhaseOrchestrator`` / ``RobustifyOrchestrator`` / raw
``structured_call`` would couple these tests to implementation details
of the wrapper — a refactor swapping the underlying orchestrator would
silently break the tests rather than reveal a real regression.
"""

import uuid
from datetime import UTC, datetime

import pytest

from rumil.models import (
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)
from rumil.processes import (
    BudgetEnvelope,
    ClaimScope,
    MapDelta,
    ProjectScope,
    QuestionScope,
    VariantSetDelta,
    ViewDelta,
)
from rumil.processes.investigator import Investigator
from rumil.processes.readback import (
    assemble_map_delta,
    assemble_variant_set_delta,
    assemble_view_delta,
)
from rumil.processes.robustifier import Robustifier
from rumil.processes.signals import (
    ConsolidateRequest,
    FocusRequest,
    PropagateFromChange,
    ReassessRequest,
    RobustifyRequest,
)
from rumil.processes.surveyor import Surveyor


async def _save_question(db, headline: str) -> Page:
    page = Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=headline,
        headline=headline,
    )
    await db.save_page(page)
    return page


async def _save_claim(db, headline: str) -> Page:
    page = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=headline,
        headline=headline,
    )
    await db.save_page(page)
    return page


async def _save_view_for_question(db, question_id: str, content: str = "view") -> Page:
    view = Page(
        page_type=PageType.VIEW,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=content,
        headline="view headline",
    )
    await db.save_page(view)
    link = PageLink(
        from_page_id=view.id,
        to_page_id=question_id,
        link_type=LinkType.VIEW_OF,
    )
    await db.save_link(link)
    return view


async def test_assemble_view_delta_picks_matching_view(tmp_db):
    baseline_run_id = str(uuid.uuid4())
    preexisting_claim_id = str(uuid.uuid4())

    await tmp_db._execute(
        tmp_db.client.table("pages").upsert(
            {
                "id": preexisting_claim_id,
                "page_type": PageType.CLAIM.value,
                "layer": PageLayer.SQUIDGY.value,
                "workspace": Workspace.RESEARCH.value,
                "content": "pre-existing",
                "headline": "pre-existing claim",
                "project_id": tmp_db.project_id,
                "run_id": baseline_run_id,
                "created_at": datetime.now(UTC).isoformat(),
            }
        )
    )

    question = await _save_question(tmp_db, "root question")
    view = await _save_view_for_question(tmp_db, question.id)
    link_to_claim = PageLink(
        from_page_id=view.id,
        to_page_id=preexisting_claim_id,
        link_type=LinkType.CITES,
    )
    await tmp_db.save_link(link_to_claim)

    delta = await assemble_view_delta(tmp_db, tmp_db.run_id, question.id)

    assert isinstance(delta, ViewDelta)
    assert delta.view_page_id == view.id
    assert {p.page_id for p in delta.new_pages} == {question.id, view.id}
    assert preexisting_claim_id in delta.cited_page_ids
    assert delta.supersedes == []

    await tmp_db._execute(
        tmp_db.client.table("page_links")
        .delete()
        .eq("from_page_id", view.id)
        .eq("to_page_id", preexisting_claim_id)
    )
    await tmp_db._execute(tmp_db.client.table("pages").delete().eq("id", preexisting_claim_id))


async def test_assemble_view_delta_none_when_no_view(tmp_db):
    question = await _save_question(tmp_db, "root question without view")
    delta = await assemble_view_delta(tmp_db, tmp_db.run_id, question.id)
    assert delta.view_page_id is None
    assert {p.page_id for p in delta.new_pages} == {question.id}


async def test_assemble_variant_set_delta(tmp_db):
    claim = await _save_claim(tmp_db, "source claim")
    variant_a = await _save_claim(tmp_db, "variant a")
    variant_b = await _save_claim(tmp_db, "variant b")
    for v in (variant_a, variant_b):
        link = PageLink(from_page_id=v.id, to_page_id=claim.id, link_type=LinkType.VARIANT)
        await tmp_db.save_link(link)

    delta = await assemble_variant_set_delta(
        tmp_db,
        tmp_db.run_id,
        claim.id,
        [variant_a.id, variant_b.id],
    )

    assert isinstance(delta, VariantSetDelta)
    assert delta.source_claim_id == claim.id
    assert delta.variant_ids == [variant_a.id, variant_b.id]
    assert {p.page_id for p in delta.new_pages} >= {
        variant_a.id,
        variant_b.id,
        claim.id,
    }


async def test_assemble_map_delta_collects_proposed_questions(tmp_db):
    root = await _save_question(tmp_db, "root")
    cross_q = await _save_question(tmp_db, "cross-cutting question")
    rel_link = PageLink(from_page_id=root.id, to_page_id=cross_q.id, link_type=LinkType.RELATED)
    await tmp_db.save_link(rel_link)

    delta = await assemble_map_delta(tmp_db, tmp_db.run_id)

    assert isinstance(delta, MapDelta)
    assert {root.id, cross_q.id}.issubset(set(delta.proposed_question_ids))
    assert rel_link.id in delta.cross_link_ids


@pytest.mark.parametrize(
    ("delta_type", "expected_kind"),
    (
        (ViewDelta, "view"),
        (VariantSetDelta, "variant_set"),
        (MapDelta, "map"),
    ),
)
def test_delta_discriminator_kinds(delta_type, expected_kind):
    kwargs = {}
    if delta_type is VariantSetDelta:
        kwargs["source_claim_id"] = "00000000-0000-0000-0000-000000000000"
    inst = delta_type(**kwargs)
    assert inst.kind == expected_kind


_VALID_STATUSES = {"complete", "incomplete", "failed"}
_SIGNAL_TYPES = (
    FocusRequest,
    ReassessRequest,
    ConsolidateRequest,
    RobustifyRequest,
    PropagateFromChange,
)


@pytest.mark.integration
async def test_investigator_produces_well_formed_result(tmp_db, question_page):
    """Real-LLM run: Investigator returns a Result with a ViewDelta of the
    right shape, regardless of whether the tiny budget let it finish."""
    proc = Investigator(tmp_db)
    result = await proc.run(
        QuestionScope(question_id=question_page.id),
        BudgetEnvelope(compute=2),
    )

    assert result.process_type == "investigator"
    assert result.run_id == tmp_db.run_id
    assert result.status in _VALID_STATUSES
    assert isinstance(result.delta, ViewDelta)
    assert result.usage.wallclock_seconds >= 0
    for sig in result.signals:
        assert isinstance(sig, _SIGNAL_TYPES)


@pytest.mark.integration
async def test_robustifier_produces_variant_set_delta(tmp_db):
    """Real-LLM run: Robustifier returns a VariantSetDelta keyed on the
    source claim it was given. Variant count is not asserted — the LLM
    may produce zero on a minimal budget."""
    claim = await _save_claim(tmp_db, "Protected bike lanes measurably increase commuter cycling.")

    proc = Robustifier(tmp_db)
    result = await proc.run(
        ClaimScope(claim_id=claim.id),
        BudgetEnvelope(compute=1),
    )

    assert result.process_type == "robustifier"
    assert result.run_id == tmp_db.run_id
    assert result.status in _VALID_STATUSES
    assert isinstance(result.delta, VariantSetDelta)
    assert result.delta.source_claim_id == claim.id


@pytest.mark.llm
async def test_surveyor_produces_map_delta_and_signals(tmp_db, question_page):
    """Real-LLM run: Surveyor returns a MapDelta plus a (possibly-empty)
    list of typed signals. The View page the surveyor commits should be
    captured in the delta."""
    proc = Surveyor(tmp_db)
    result = await proc.run(
        ProjectScope(project_id=tmp_db.project_id),
        BudgetEnvelope(compute=1),
    )

    assert result.process_type == "surveyor"
    assert result.run_id == tmp_db.run_id
    assert result.status in _VALID_STATUSES
    assert isinstance(result.delta, MapDelta)

    if result.status == "complete":
        assert result.delta.map_view_id is not None

    for sig in result.signals:
        assert isinstance(sig, _SIGNAL_TYPES)
