"""Tests for RefinementContext and the end-to-end generative orchestrator.

Non-LLM: RefinementContext rendering contains the current spec plus the
last-N iteration triples reconstructed via GENERATED_FROM + CRITIQUE_OF.

Integration: GenerativeOrchestrator.run produces a visible artefact given
a small, well-scoped request.
"""

import pytest
import pytest_asyncio

from rumil.calls.context_builders import RefinementContext
from rumil.calls.stages import CallInfra
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
from rumil.moves.base import MoveState
from rumil.moves.regenerate_and_critique import RegenerateAndCritiquePayload
from rumil.moves.regenerate_and_critique import execute as regenerate_and_critique
from rumil.orchestrators.generative import GenerativeOrchestrator
from rumil.tracing.tracer import CallTrace


async def _make_page(tmp_db, headline, *, page_type=PageType.CLAIM, hidden=False, extra=None):
    page = Page(
        page_type=page_type,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=f"Content for: {headline}",
        headline=headline,
        hidden=hidden,
        extra=extra or {},
    )
    await tmp_db.save_page(page)
    return page


@pytest_asyncio.fixture
async def refinement_task(tmp_db):
    return await _make_page(
        tmp_db,
        "Refinement task",
        page_type=PageType.QUESTION,
        hidden=True,
    )


async def _seed_spec(tmp_db, task, headlines):
    specs = []
    for h in headlines:
        spec = await _make_page(tmp_db, h, page_type=PageType.SPEC_ITEM, hidden=True)
        await tmp_db.save_link(
            PageLink(
                from_page_id=spec.id,
                to_page_id=task.id,
                link_type=LinkType.SPEC_OF,
            )
        )
        specs.append(spec)
    return specs


async def _seed_iteration(tmp_db, task, spec_items, *, artefact_headline, grade, issues):
    artefact = await _make_page(
        tmp_db,
        artefact_headline,
        page_type=PageType.ARTEFACT,
        hidden=True,
    )
    await tmp_db.save_link(
        PageLink(
            from_page_id=artefact.id,
            to_page_id=task.id,
            link_type=LinkType.ARTEFACT_OF,
        )
    )
    for spec in spec_items:
        await tmp_db.save_link(
            PageLink(
                from_page_id=artefact.id,
                to_page_id=spec.id,
                link_type=LinkType.GENERATED_FROM,
            )
        )
    critique = await _make_page(
        tmp_db,
        f"Critique of {artefact_headline}",
        page_type=PageType.JUDGEMENT,
        hidden=True,
        extra={"grade": grade, "issues": issues},
    )
    await tmp_db.save_link(
        PageLink(
            from_page_id=critique.id,
            to_page_id=artefact.id,
            link_type=LinkType.CRITIQUE_OF,
        )
    )
    return artefact, critique


def _make_infra(tmp_db, task_id):
    call = Call(
        call_type=CallType.REFINE_SPEC,
        workspace=Workspace.RESEARCH,
        scope_page_id=task_id,
        status=CallStatus.PENDING,
    )
    return CallInfra(
        question_id=task_id,
        call=call,
        db=tmp_db,
        trace=CallTrace(call.id, tmp_db),
        state=MoveState(call, tmp_db),
    )


async def test_refinement_context_with_no_iterations(tmp_db, refinement_task):
    await _seed_spec(tmp_db, refinement_task, ["Rule A", "Rule B"])

    infra = _make_infra(tmp_db, refinement_task.id)
    ctx = await RefinementContext().build_context(infra)

    assert "Current spec (2 items)" in ctx.context_text
    assert "Rule A" in ctx.context_text
    assert "Rule B" in ctx.context_text
    assert "no iterations yet" in ctx.context_text.lower()


async def test_refinement_context_renders_past_triples(tmp_db, refinement_task):
    current = await _seed_spec(tmp_db, refinement_task, ["Current rule A", "Current rule B"])

    older_spec = await _make_page(
        tmp_db, "Old rule superseded", page_type=PageType.SPEC_ITEM, hidden=True
    )
    await _seed_iteration(
        tmp_db,
        refinement_task,
        [older_spec, current[0]],
        artefact_headline="Draft v1",
        grade=4,
        issues=["Missing key detail", "Too long"],
    )

    await _seed_iteration(
        tmp_db,
        refinement_task,
        current,
        artefact_headline="Draft v2",
        grade=7,
        issues=["Minor wording"],
    )

    infra = _make_infra(tmp_db, refinement_task.id)
    ctx = await RefinementContext().build_context(infra)

    v1_pos = ctx.context_text.index("Draft v1")
    v2_pos = ctx.context_text.index("Draft v2")
    assert v1_pos < v2_pos

    assert "Old rule superseded" in ctx.context_text
    assert "**Grade:** 4/10" in ctx.context_text
    assert "**Grade:** 7/10" in ctx.context_text
    assert "Missing key detail" in ctx.context_text
    assert "Minor wording" in ctx.context_text


async def test_refinement_context_respects_window(tmp_db, refinement_task):
    """With more artefacts than the window, only the most recent N render."""
    current = await _seed_spec(tmp_db, refinement_task, ["Rule A"])
    for label in ("v1", "v2", "v3", "v4"):
        await _seed_iteration(
            tmp_db,
            refinement_task,
            current,
            artefact_headline=label,
            grade=5,
            issues=[],
        )

    infra = _make_infra(tmp_db, refinement_task.id)
    ctx = await RefinementContext(window=2).build_context(infra)

    assert "v3" in ctx.context_text
    assert "v4" in ctx.context_text
    assert "v1" not in ctx.context_text
    assert "v2" not in ctx.context_text


async def test_regenerate_and_critique_errors_when_budget_below_three(tmp_db, refinement_task):
    """Budget guard must be atomic — fewer than 3 units means no sub-calls fire
    and no partial artefact is produced. Exercised without the LLM by setting
    a tight budget before calling."""
    await _seed_spec(tmp_db, refinement_task, ["Rule A"])

    parent = Call(
        call_type=CallType.REFINE_SPEC,
        workspace=Workspace.RESEARCH,
        scope_page_id=refinement_task.id,
        status=CallStatus.PENDING,
    )
    await tmp_db.save_call(parent)

    total, used = await tmp_db.get_budget()
    await tmp_db.consume_budget(total - used - 2)  # leave exactly 2 units

    result = await regenerate_and_critique(
        RegenerateAndCritiquePayload(reason="should be rejected"),
        parent,
        tmp_db,
    )
    assert "budget" in result.message.lower()
    assert result.created_page_id is None

    artefacts = await tmp_db.get_pages(page_type=PageType.ARTEFACT, include_hidden=True)
    assert artefacts == []


@pytest.mark.integration
async def test_regenerate_and_critique_produces_artefact_and_two_critiques(tmp_db, refinement_task):
    """Happy path: the move fires the artefact + both critique sub-calls and
    links them up. Verifies budget consumption and CRITIQUE_OF cardinality."""
    await tmp_db.init_budget(8)
    await _seed_spec(
        tmp_db,
        refinement_task,
        [
            "Keep it short",
            "Use plain language",
            "No headings, prose only",
        ],
    )

    parent = Call(
        call_type=CallType.REFINE_SPEC,
        workspace=Workspace.RESEARCH,
        scope_page_id=refinement_task.id,
        status=CallStatus.PENDING,
    )
    await tmp_db.save_call(parent)

    _total, used_before = await tmp_db.get_budget()
    result = await regenerate_and_critique(
        RegenerateAndCritiquePayload(reason="initial"),
        parent,
        tmp_db,
    )
    _total, used_after = await tmp_db.get_budget()
    assert used_after - used_before == 3

    assert "Regenerated artefact" in result.message

    artefact = await tmp_db.latest_artefact_for_task(refinement_task.id)
    assert artefact is not None
    assert artefact.page_type == PageType.ARTEFACT

    inbound = await tmp_db.get_links_to(artefact.id)
    critique_links = [l for l in inbound if l.link_type == LinkType.CRITIQUE_OF]
    assert len(critique_links) == 2

    critiques = await tmp_db.get_pages_by_ids([l.from_page_id for l in critique_links])
    kinds = {p.provenance_call_type for p in critiques.values()}
    assert kinds == {
        CallType.CRITIQUE_ARTEFACT.value,
        CallType.CRITIQUE_ARTEFACT_REQUEST_ONLY.value,
    }
    for crit in critiques.values():
        assert crit.page_type == PageType.JUDGEMENT
        assert "grade" in crit.extra


async def test_resume_errors_on_unknown_task(tmp_db):
    """resume() must raise when the given task_id doesn't exist."""
    orchestrator = GenerativeOrchestrator(tmp_db)
    import uuid as _uuid

    fake_id = str(_uuid.uuid4())
    with pytest.raises(ValueError, match="not found"):
        await orchestrator.resume(fake_id)


async def test_resume_errors_when_target_is_not_a_question(tmp_db):
    """resume() rejects non-question scopes — refuses to operate on, say, a claim."""
    claim = Page(
        page_type=PageType.CLAIM,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="unrelated claim",
        headline="unrelated",
    )
    await tmp_db.save_page(claim)

    orchestrator = GenerativeOrchestrator(tmp_db)
    with pytest.raises(ValueError, match="expected question"):
        await orchestrator.resume(claim.id)


@pytest.mark.integration
async def test_generative_orchestrator_produces_visible_artefact(tmp_db):
    """Tight request → visible ARTEFACT at the end, even if the refiner runs
    out of moves. Uses a concrete request small enough for Haiku to handle
    reliably within a modest budget. Each regeneration costs 3 (artefact +
    two critiques), so 18 covers a few iterations plus setup."""
    await tmp_db.init_budget(18)

    orchestrator = GenerativeOrchestrator(tmp_db, refine_max_rounds=4)
    result = await orchestrator.run(
        (
            "Write a three-bullet checklist for a team introducing a new "
            "intern to their first dataset: what files to open first, "
            "who to talk to, and where to put their first analysis. "
            "Keep it to three bullets, one line each."
        ),
        headline="Intern onboarding checklist",
    )

    assert result.task_id
    assert result.artefact_id is not None

    artefact = await tmp_db.get_page(result.artefact_id)
    assert artefact is not None
    assert artefact.page_type == PageType.ARTEFACT
    assert artefact.hidden is False
    assert len(artefact.content) > 30

    task = await tmp_db.get_page(result.task_id)
    assert task is not None
    assert task.hidden is True
