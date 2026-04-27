"""Pin that judge_prompt_is_current catches stale judge prompts / versions.

This is the logic status.py uses to detect judgment rows that were
cached before a versus-*.md edit or a BLIND_JUDGE_VERSION bump. Without
it, prompt edits silently orphan existing rows under new keys while
status.py keeps reporting "all current".
"""

from __future__ import annotations

import sys
from pathlib import Path

_VERSUS_SRC = Path(__file__).resolve().parents[1] / "versus" / "src"
if str(_VERSUS_SRC) not in sys.path:
    sys.path.insert(0, str(_VERSUS_SRC))

from versus.versions import BLIND_JUDGE_VERSION, JUDGE_PROMPT_VERSION  # noqa: E402

from versus import judge  # noqa: E402

_SAMPLING = {"temperature": 0.0, "max_tokens": 1024}


def test_blind_openrouter_judge_model_at_current_version_is_current():
    jm = judge.compose_blind_judge_model("openai/gpt-5", "general_quality", _SAMPLING)
    assert judge.judge_prompt_is_current(jm, "general_quality") is True


def test_blind_anthropic_judge_model_at_current_version_is_current():
    jm = judge.compose_blind_judge_model("claude-opus-4-7", "general_quality", _SAMPLING)
    assert judge.judge_prompt_is_current(jm, "general_quality") is True


def test_stale_phash_is_flagged():
    jm = f"openai/gpt-5:general_quality:pdeadbeef:v{BLIND_JUDGE_VERSION}"
    assert judge.judge_prompt_is_current(jm, "general_quality") is False


def test_stale_version_is_flagged():
    current_ph = judge.compute_judge_prompt_hash("general_quality")
    jm = f"openai/gpt-5:general_quality:p{current_ph}:v{BLIND_JUDGE_VERSION - 1}"
    assert judge.judge_prompt_is_current(jm, "general_quality") is False


def test_legacy_unversioned_judge_model_is_flagged():
    assert judge.judge_prompt_is_current("openai/gpt-5", "general_quality") is False


def test_legacy_anthropic_text_keys_read_stale():
    current_ph = judge.compute_judge_prompt_hash("general_quality")
    jm = f"anthropic:claude-opus-4-7:p{current_ph}:v{JUDGE_PROMPT_VERSION}:sdeadbeef"
    assert judge.judge_prompt_is_current(jm, "general_quality") is False


def test_rumil_ws_judge_uses_tools_shell_hash():
    current_ph_tools = judge.compute_judge_prompt_hash("general_quality", with_tools=True)
    jm = (
        f"rumil:ws:claude-opus-4-7:ws_short:general_quality:"
        f"p{current_ph_tools}:v{BLIND_JUDGE_VERSION}:tfeedface"
    )
    assert judge.judge_prompt_is_current(jm, "general_quality") is True


def test_rumil_ws_with_blind_phash_is_flagged():
    blind_ph = judge.compute_judge_prompt_hash("general_quality", with_tools=False)
    tools_ph = judge.compute_judge_prompt_hash("general_quality", with_tools=True)
    assert blind_ph != tools_ph
    jm = (
        f"rumil:ws:claude-opus-4-7:ws_short:general_quality:"
        f"p{blind_ph}:v{BLIND_JUDGE_VERSION}:tfeedface"
    )
    assert judge.judge_prompt_is_current(jm, "general_quality") is False


def test_rumil_ws_judge_at_wrong_version_is_flagged():
    current_ph = judge.compute_judge_prompt_hash("general_quality", with_tools=True)
    jm = (
        f"rumil:ws:claude-opus-4-7:ws_short:general_quality:"
        f"p{current_ph}:v{BLIND_JUDGE_VERSION - 1}:tfeedface"
    )
    assert judge.judge_prompt_is_current(jm, "general_quality") is False


def test_unknown_dimension_is_flagged():
    jm = f"openai/gpt-5:no_such_dim:pabcdef01:v{BLIND_JUDGE_VERSION}"
    assert judge.judge_prompt_is_current(jm, "no_such_dimension_prompt") is False
