"""Pin the dual-shape helpers that bridge legacy / new versus_config rows.

``row_prompt_hash`` and ``is_rumil_row`` live in ``versus.versus_config``
and are read by the API router (``rumil.api.versus_router``) plus any
analysis layer that needs to render a judgment row regardless of which
config dict shape the DB carries — pre-#424 flat (``variant`` /
``prompts.shell_hash``) vs. post-#424 nested (``workflow`` / ``task``
subdicts). These tests pin the read-side projection so a new-shape row
doesn't KeyError or silently lose its rumil tint when the router hits it.
"""

from __future__ import annotations

import sys
from pathlib import Path

_VERSUS_SRC = Path(__file__).resolve().parents[1] / "versus" / "src"
if str(_VERSUS_SRC) not in sys.path:
    sys.path.insert(0, str(_VERSUS_SRC))

from versus.versus_config import is_rumil_row, row_prompt_hash  # noqa: E402

_LEGACY_BLIND = {
    "variant": "blind",
    "model": "claude-opus-4-7",
    "dimension": "general_quality",
    "prompts": {"shell_hash": "deadbeef"},
}

_LEGACY_ORCH = {
    "variant": "orch",
    "model": "claude-opus-4-7",
    "dimension": "general_quality",
    "prompts": {"shell_hash": "deadbeef"},
    "tool_descriptions_hash": "11111111",
    "pair_surface_hash": "22222222",
    "workspace_id": "abcd1234",
    "budget": 4,
    "closer_hash": "33333333",
}

_NEW_BLIND = {
    "model": "claude-opus-4-7",
    "model_config": {"temperature": None, "max_tokens": 1024},
    "workflow": {"kind": "blind"},
    "task": {"kind": "judge_pair", "dimension": "general_quality", "prompt_hash": "deadbeef"},
}

_NEW_TWO_PHASE = {
    "model": "claude-opus-4-7",
    "model_config": {"temperature": None, "max_tokens": 1024},
    "workflow": {"kind": "two_phase", "budget": 4},
    "task": {
        "kind": "judge_pair",
        "dimension": "general_quality",
        "prompt_hash": "deadbeef",
        "tool_prompt_hash": "11111111",
        "pair_surface_hash": "22222222",
        "closer_hash": "33333333",
    },
}


def test_row_prompt_hash_legacy_shape():
    assert row_prompt_hash(_LEGACY_BLIND) == "deadbeef"
    assert row_prompt_hash(_LEGACY_ORCH) == "deadbeef"


def test_row_prompt_hash_new_shape():
    assert row_prompt_hash(_NEW_BLIND) == "deadbeef"
    assert row_prompt_hash(_NEW_TWO_PHASE) == "deadbeef"


def test_row_prompt_hash_missing_returns_none():
    # Legacy row missing the prompts subdict.
    assert row_prompt_hash({"variant": "blind", "model": "m"}) is None
    # New row with task subdict but no prompt_hash.
    assert row_prompt_hash({"workflow": {"kind": "blind"}, "task": {"dimension": "x"}}) is None
    # New row whose task is None.
    assert row_prompt_hash({"workflow": {"kind": "blind"}, "task": None}) is None
    # Empty dict.
    assert row_prompt_hash({}) is None


def test_is_rumil_row_legacy_shape_uses_prefix():
    # Legacy rows didn't carry a ``workflow`` subdict; the variant was
    # encoded only in the judge_model display string.
    assert is_rumil_row(_LEGACY_BLIND, "blind:claude-opus-4-7:dim:c12345678") is False
    assert is_rumil_row(_LEGACY_ORCH, "rumil:orch:claude-opus-4-7:dim:c12345678") is True
    assert is_rumil_row(_LEGACY_ORCH, "rumil:ws:claude-opus-4-7:dim:c12345678") is True


def test_is_rumil_row_new_shape_reads_workflow_kind():
    # Display string is "judge_pair/<workflow>:..." but the workflow
    # field is the source of truth — the prefix check would say "False"
    # for both blind and two_phase rows, which is the bug.
    assert is_rumil_row(_NEW_BLIND, "judge_pair/blind:claude-opus-4-7:c12345678") is False
    assert is_rumil_row(_NEW_TWO_PHASE, "judge_pair/two_phase:claude-opus-4-7:c12345678") is True


def test_is_rumil_row_new_shape_unknown_workflow_kind_is_rumil():
    # Anything that isn't ``"blind"`` is treated as a rumil workflow —
    # so a hypothetical future ``draft_and_edit`` workflow lights up
    # the rumil tint without code changes here.
    cfg = {**_NEW_TWO_PHASE, "workflow": {"kind": "draft_and_edit", "budget": 4}}
    assert is_rumil_row(cfg, "judge_pair/draft_and_edit:claude-opus-4-7:c12345678") is True
