"""Test that the orchestrator re-prioritizes when budget remains after dispatches complete."""

import pytest

from rumil.models import CallType
from rumil.orchestrator import Orchestrator, create_root_question


@pytest.mark.llm
async def test_reprioritization_fires_when_budget_remains(tmp_db):
    """
    When a run completes with budget still remaining, investigate_question should
    re-prioritize to redirect leftover budget.

    We give a budget large enough that at least one scout round leaves leftover
    budget (scouts often finish in fewer calls than allocated), which triggers
    the re-prioritization path: if leftover > 0 and actually_spent > 0, the
    orchestrator calls investigate_question again, producing a second
    prioritization call.
    """
    await tmp_db.init_budget(4)
    question_id = await create_root_question(
        "What are the main causes of the French Revolution?", tmp_db
    )

    orch = Orchestrator(tmp_db)
    await orch.investigate_question(question_id, budget=4)

    calls = await tmp_db.get_calls_for_run(tmp_db.run_id)
    prioritization_calls = [c for c in calls if c.call_type == CallType.PRIORITIZATION]

    assert len(prioritization_calls) >= 2, (
        f"Expected at least 2 prioritization calls (initial + re-prioritization), "
        f"got {len(prioritization_calls)}. "
        f"All call types: {[c.call_type.value for c in calls]}"
    )
