"""Unit tests for CritiqueFirstOrchestrator.

All tests mock the LLM / workspace-updating helpers entirely — no API
calls. We verify the loop's dispatch shape: what runs when, and in which
order, under different DB states.
"""

from collections.abc import Sequence

import pytest

from rumil.database import DB
from rumil.models import (
    CallType,
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)
from rumil.orchestrators.critique_first import (
    MIN_CONSIDERATIONS,
    CritiqueFirstOrchestrator,
)


async def _noop(*args, **kwargs):
    return None


def _make_question_page(project_id: str) -> Page:
    return Page(
        page_type=PageType.QUESTION,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content="Q?",
        headline="Q?",
        project_id=project_id,
    )


def _make_consideration_pairs(n: int, question_id: str) -> list[tuple[Page, PageLink]]:
    """Produce n (claim, link) pairs as if they were considerations on question_id."""
    out: list[tuple[Page, PageLink]] = []
    for i in range(n):
        claim = Page(
            page_type=PageType.CLAIM,
            layer=PageLayer.SQUIDGY,
            workspace=Workspace.RESEARCH,
            content=f"claim {i}",
            headline=f"claim {i}",
        )
        link = PageLink(
            from_page_id=claim.id,
            to_page_id=question_id,
            link_type=LinkType.CONSIDERATION,
        )
        out.append((claim, link))
    return out


@pytest.fixture
def patched_helpers(mocker):
    """Patch all the helper functions CritiqueFirstOrchestrator calls.

    Patches target the orchestrator's own namespace (critique_first) so the
    patches don't leak into other modules.
    """
    find = mocker.patch(
        "rumil.orchestrators.critique_first.find_considerations_until_done",
        side_effect=_noop,
    )
    assess = mocker.patch(
        "rumil.orchestrators.critique_first.assess_question",
        side_effect=_noop,
    )
    create_view = mocker.patch(
        "rumil.orchestrators.critique_first.create_view_for_question",
        side_effect=_noop,
    )
    update_view = mocker.patch(
        "rumil.orchestrators.critique_first.update_view_for_question",
        side_effect=_noop,
    )
    return {
        "find": find,
        "assess": assess,
        "create_view": create_view,
        "update_view": update_view,
    }


@pytest.fixture
def patched_scouts(mocker):
    """Stub ScoutCHowTrueCall and ScoutCHowFalseCall so no LLM work happens."""
    true_runs: list[str] = []
    false_runs: list[str] = []
    created_counter = {"n": 0}

    def _fresh_page() -> str:
        created_counter["n"] += 1
        return f"scout-page-{created_counter['n']}"

    class _FakeScoutResult:
        def __init__(self, page_ids: Sequence[str]):
            self.created_page_ids = list(page_ids)

    class _FakeScout:
        def __init__(
            self,
            question_id: str,
            call,
            db,
            *,
            broadcaster=None,
            max_rounds: int = 2,
            fruit_threshold: int = 4,
        ):
            self._question_id = question_id
            self._call_type = call.call_type
            self.result = _FakeScoutResult([_fresh_page()])

        async def run(self):
            if self._call_type == CallType.SCOUT_C_HOW_TRUE:
                true_runs.append(self._question_id)
            else:
                false_runs.append(self._question_id)

    mocker.patch(
        "rumil.orchestrators.critique_first.ScoutCHowTrueCall",
        _FakeScout,
    )
    mocker.patch(
        "rumil.orchestrators.critique_first.ScoutCHowFalseCall",
        _FakeScout,
    )
    return {
        "true_runs": true_runs,
        "false_runs": false_runs,
    }


async def test_sparse_question_first_dispatch_is_find_considerations(
    tmp_db: DB,
    patched_helpers,
    patched_scouts,
):
    """With zero considerations, the very first dispatch must be find_considerations."""
    qp = _make_question_page(tmp_db.project_id)
    await tmp_db.save_page(qp)

    orch = CritiqueFirstOrchestrator(tmp_db)
    await orch.run(qp.id)

    assert patched_helpers["find"].call_count >= 1
    first_call_qid = patched_helpers["find"].call_args_list[0].args[0]
    assert first_call_qid == qp.id
    assert patched_scouts["true_runs"] == []
    assert patched_scouts["false_runs"] == []


async def test_dense_question_runs_critique_scouts_before_find(
    tmp_db: DB,
    mocker,
    patched_helpers,
    patched_scouts,
):
    """Once >= MIN_CONSIDERATIONS considerations exist, scouts run before find_considerations."""
    qp = _make_question_page(tmp_db.project_id)
    await tmp_db.save_page(qp)

    pairs = _make_consideration_pairs(MIN_CONSIDERATIONS, qp.id)
    mocker.patch.object(
        DB,
        "get_considerations_for_question",
        return_value=pairs,
    )
    mocker.patch.object(DB, "get_judgements_for_question", return_value=[])

    await tmp_db.consume_budget(96)

    orch = CritiqueFirstOrchestrator(tmp_db)
    await orch.run(qp.id)

    total_scout_runs = len(patched_scouts["true_runs"]) + len(patched_scouts["false_runs"])
    assert total_scout_runs >= 2
    assert len(patched_scouts["true_runs"]) >= 1
    assert len(patched_scouts["false_runs"]) >= 1
    assert patched_scouts["true_runs"][0] == qp.id
    assert patched_scouts["false_runs"][0] == qp.id


async def test_find_considerations_receives_scout_page_ids_as_context(
    tmp_db: DB,
    mocker,
    patched_helpers,
    patched_scouts,
):
    """After scouts create pages, the next find_considerations call must pass
    those page IDs through as context_page_ids."""
    qp = _make_question_page(tmp_db.project_id)
    await tmp_db.save_page(qp)

    pairs = _make_consideration_pairs(MIN_CONSIDERATIONS, qp.id)
    mocker.patch.object(DB, "get_considerations_for_question", return_value=pairs)
    mocker.patch.object(DB, "get_judgements_for_question", return_value=[])

    await tmp_db.consume_budget(96)

    orch = CritiqueFirstOrchestrator(tmp_db)
    await orch.run(qp.id)

    find_calls_with_ctx = [
        c for c in patched_helpers["find"].call_args_list if c.kwargs.get("context_page_ids")
    ]
    assert find_calls_with_ctx, (
        "Expected at least one find_considerations call to receive context_page_ids, "
        f"got calls: {patched_helpers['find'].call_args_list}"
    )
    ctx_ids = find_calls_with_ctx[0].kwargs["context_page_ids"]
    assert all(cid.startswith("scout-page-") for cid in ctx_ids)
    assert len(ctx_ids) >= 2


async def test_assess_runs_each_cycle(
    tmp_db: DB,
    mocker,
    patched_helpers,
    patched_scouts,
):
    """Every loop cycle that performs work should also run assess_question."""
    qp = _make_question_page(tmp_db.project_id)
    await tmp_db.save_page(qp)

    pairs = _make_consideration_pairs(MIN_CONSIDERATIONS, qp.id)
    mocker.patch.object(DB, "get_considerations_for_question", return_value=pairs)
    mocker.patch.object(DB, "get_judgements_for_question", return_value=[])

    await tmp_db.consume_budget(92)

    orch = CritiqueFirstOrchestrator(tmp_db)
    await orch.run(qp.id)

    assert patched_helpers["assess"].call_count >= 1
    for c in patched_helpers["assess"].call_args_list:
        assert c.args[0] == qp.id


async def test_budget_exhaustion_terminates_loop(
    tmp_db: DB,
    mocker,
    patched_helpers,
    patched_scouts,
):
    """When budget is exhausted the loop must stop — it must not keep
    calling find_considerations forever.

    We simulate real budget consumption by draining the budget from inside
    find_considerations. The orchestrator must observe the drain and exit
    cleanly on the next budget check.
    """
    qp = _make_question_page(tmp_db.project_id)
    await tmp_db.save_page(qp)

    pairs = _make_consideration_pairs(MIN_CONSIDERATIONS, qp.id)
    mocker.patch.object(DB, "get_considerations_for_question", return_value=pairs)
    mocker.patch.object(DB, "get_judgements_for_question", return_value=[])

    call_count = {"n": 0}

    async def drain(question_id, db, **kwargs):
        call_count["n"] += 1
        await db.consume_budget(await db.budget_remaining())

    patched_helpers["find"].side_effect = drain

    await tmp_db.consume_budget(95)
    assert await tmp_db.budget_remaining() == 5

    orch = CritiqueFirstOrchestrator(tmp_db)
    await orch.run(qp.id)

    assert call_count["n"] == 1
    assert await tmp_db.budget_remaining() == 0
