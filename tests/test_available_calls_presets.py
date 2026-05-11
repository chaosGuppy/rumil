"""Regression tests pinning the contents of the named available-calls presets.

Presets shape what scouts/dispatches the two-phase orchestrator can use, so
silent edits to the dicts can drastically change run behaviour without any
test failing elsewhere. These tests lock in the load-bearing membership
decisions — particularly the ones motivated by self-improvement diagnostics —
so a future edit at least has to delete a test on its way out.
"""

from rumil.available_calls import AVAILABLE_CALLS_PRESETS
from rumil.models import CallType


def test_default_preset_includes_scout_web_questions_in_initial_fanout():
    """Empirical questions need a way to surface checkable web lookups during
    initial fan-out; without ``scout_web_questions`` the orchestrator falls
    back on ``scout_factchecks``, which is structurally guaranteed to fail
    when no claims exist yet (its prompt explicitly redirects to
    scout_web_questions in that case).
    """
    preset = AVAILABLE_CALLS_PRESETS["default"]
    assert CallType.SCOUT_WEB_QUESTIONS in preset.initial_prioritization_scouts


def test_simple_and_multi_subquestion_presets_unchanged_for_scout_web_questions():
    """Sibling presets already had this in their initial fan-out; this test
    documents that ``default`` is now consistent with them."""
    for name in ("simple", "multi-subquestion"):
        preset = AVAILABLE_CALLS_PRESETS[name]
        assert CallType.SCOUT_WEB_QUESTIONS in preset.initial_prioritization_scouts, (
            f"{name} preset lost SCOUT_WEB_QUESTIONS from initial fan-out"
        )
