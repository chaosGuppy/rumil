"""Regression tests for wave-7 bug: FLAG_ISSUE not surfacing to the LLM.

Covers two layers of surfacing that must both work for the model to actually
use the flag_issue tool on a call:

1. Move-list surfacing: ``get_moves_for_call(...)`` includes ``FLAG_ISSUE``
   when the feature flag is on (and doesn't when it's off). Also checks the
   env-var propagation path (``ENABLE_FLAG_ISSUE=true`` → setting → move list),
   since that was the suspected config-propagation regression.
2. Prompt surfacing: ``build_system_prompt(...)`` appends a short addendum
   priming the model to use ``flag_issue`` when, and only when, the move is
   actually in the list for that call.
"""

import os
import subprocess
import sys

import pytest

from rumil.available_moves import get_moves_for_call
from rumil.llm import build_system_prompt
from rumil.models import CallType, MoveType
from rumil.settings import override_settings

_CALL_TYPES_WITH_FLAG_ISSUE = (
    CallType.FIND_CONSIDERATIONS,
    CallType.ASSESS,
    CallType.INGEST,
    CallType.SCOUT_SUBQUESTIONS,
    CallType.SCOUT_HYPOTHESES,
    CallType.SCOUT_ESTIMATES,
    CallType.SCOUT_ANALOGIES,
    CallType.SCOUT_FACTCHECKS,
    CallType.WEB_RESEARCH,
)


@pytest.mark.parametrize("call_type", _CALL_TYPES_WITH_FLAG_ISSUE)
def test_flag_issue_surfaces_in_moves_when_enabled(call_type):
    with override_settings(enable_flag_issue=True):
        moves = get_moves_for_call(call_type)
    assert MoveType.FLAG_ISSUE in moves


@pytest.mark.parametrize("call_type", _CALL_TYPES_WITH_FLAG_ISSUE)
def test_flag_issue_absent_from_moves_when_disabled(call_type):
    with override_settings(enable_flag_issue=False):
        moves = get_moves_for_call(call_type)
    assert MoveType.FLAG_ISSUE not in moves


def test_env_var_propagates_to_moves_for_find_considerations():
    """ENABLE_FLAG_ISSUE=true should flow through settings to get_moves_for_call.

    This is the diagnostic the wave-7 smoke test doc asked for: confirm the
    env var reaches the move list in a fresh interpreter (so no cached
    settings from another test can mask a regression).
    """
    script = (
        "from rumil.settings import get_settings; "
        "from rumil.available_moves import get_moves_for_call; "
        "from rumil.models import CallType, MoveType; "
        "s = get_settings(); "
        "moves = get_moves_for_call(CallType.FIND_CONSIDERATIONS); "
        "print(s.enable_flag_issue, MoveType.FLAG_ISSUE in moves)"
    )
    env = {**os.environ, "ENABLE_FLAG_ISSUE": "true"}
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "True True", (
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


@pytest.mark.parametrize("call_type", _CALL_TYPES_WITH_FLAG_ISSUE)
def test_flag_issue_addendum_in_system_prompt_when_enabled(call_type):
    with override_settings(enable_flag_issue=True):
        prompt = build_system_prompt(call_type.value)
    assert "flag_issue" in prompt
    assert "Meta-feedback" in prompt


@pytest.mark.parametrize("call_type", _CALL_TYPES_WITH_FLAG_ISSUE)
def test_flag_issue_addendum_absent_from_system_prompt_when_disabled(call_type):
    with override_settings(enable_flag_issue=False):
        prompt = build_system_prompt(call_type.value)
    assert "Meta-feedback" not in prompt


def test_flag_issue_addendum_not_added_for_calls_without_flag_issue_move():
    """Empty-preset calls (e.g. PRIORITIZATION) must not get the addendum even
    when the global flag is on — otherwise turning the feature on leaks stale
    priming onto calls that have no way to actually flag."""
    with override_settings(enable_flag_issue=True):
        moves = get_moves_for_call(CallType.PRIORITIZATION)
        prompt = build_system_prompt("two_phase_initial_prioritization")
    assert MoveType.FLAG_ISSUE not in moves
    assert "Meta-feedback" not in prompt
