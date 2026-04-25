"""Pin that judge_prompt_is_current catches stale judge prompts / versions.

This is the logic status.py uses to detect judgment rows that were
cached before a versus-*.md edit or a JUDGE_PROMPT_VERSION /
BLIND_JUDGE_VERSION bump. Without it, prompt edits silently orphan
existing rows under new keys while status.py keeps reporting "all
current" — the bug the audit flagged.
"""

from __future__ import annotations

import sys
from pathlib import Path

_VERSUS_SRC = Path(__file__).resolve().parents[1] / "versus" / "src"
if str(_VERSUS_SRC) not in sys.path:
    sys.path.insert(0, str(_VERSUS_SRC))

from versus.versions import BLIND_JUDGE_VERSION, JUDGE_PROMPT_VERSION  # noqa: E402

from versus import judge  # noqa: E402


def test_openrouter_judge_model_at_current_version_is_current():
    jm = judge.compose_judge_model("openai/gpt-5", "general_quality")
    assert judge.judge_prompt_is_current(jm, "general_quality") is True


def test_stale_phash_is_flagged():
    jm = f"openai/gpt-5:pdeadbeef:v{JUDGE_PROMPT_VERSION}"
    assert judge.judge_prompt_is_current(jm, "general_quality") is False


def test_stale_version_is_flagged():
    current_ph = judge.compute_judge_prompt_hash("general_quality")
    jm = f"openai/gpt-5:p{current_ph}:v{JUDGE_PROMPT_VERSION - 1}"
    assert judge.judge_prompt_is_current(jm, "general_quality") is False


def test_legacy_unversioned_judge_model_is_flagged():
    # Pre-hash rows can't satisfy any current expectation.
    assert judge.judge_prompt_is_current("openai/gpt-5", "general_quality") is False


def test_rumil_ws_judge_uses_blind_judge_version():
    current_ph = judge.compute_judge_prompt_hash("general_quality")
    jm = f"rumil:ws:claude-opus-4-7:ws_short:general_quality:p{current_ph}:v{BLIND_JUDGE_VERSION}:tfeedface"
    assert judge.judge_prompt_is_current(jm, "general_quality") is True


def test_rumil_ws_judge_at_wrong_version_is_flagged():
    current_ph = judge.compute_judge_prompt_hash("general_quality")
    jm = f"rumil:ws:claude-opus-4-7:ws_short:general_quality:p{current_ph}:v{BLIND_JUDGE_VERSION - 1}:tfeedface"
    assert judge.judge_prompt_is_current(jm, "general_quality") is False


def test_unknown_dimension_is_flagged():
    # Dimension file was deleted -- can't match anything current.
    jm = f"openai/gpt-5:pabcdef01:v{JUDGE_PROMPT_VERSION}"
    assert judge.judge_prompt_is_current(jm, "no_such_dimension_prompt") is False
