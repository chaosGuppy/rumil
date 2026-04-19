"""Tests for the --pin-prompt override path — pinning a prompt's
content at runtime so build_system_prompt uses it instead of the
filesystem copy.
"""

from __future__ import annotations

import pytest

from rumil.llm import build_system_prompt, pin_prompt_content


@pytest.fixture(autouse=True)
def _reset_overrides():
    from rumil.llm import _PROMPT_OVERRIDES

    original = dict(_PROMPT_OVERRIDES)
    yield
    _PROMPT_OVERRIDES.clear()
    _PROMPT_OVERRIDES.update(original)


def test_pin_prompt_content_overrides_file():
    pin_prompt_content("preamble", "PINNED PREAMBLE")
    result = build_system_prompt("find_considerations")
    assert "PINNED PREAMBLE" in result


def test_unpinned_prompts_read_from_file():
    pin_prompt_content("preamble", "PINNED PREAMBLE")
    result = build_system_prompt("find_considerations")
    # find_considerations.md is NOT pinned — its real content must load
    # (non-empty, and not the pinned preamble body)
    assert "PINNED PREAMBLE" in result
    assert len(result) > len("PINNED PREAMBLE") * 2


def test_pin_survives_rebuild():
    pin_prompt_content("citations", "PINNED CITATIONS")
    first = build_system_prompt("find_considerations")
    second = build_system_prompt("find_considerations")
    assert first == second
    assert "PINNED CITATIONS" in first
