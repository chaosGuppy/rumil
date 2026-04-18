"""Guard against accidental deletion of the wave-7 prompt fixes.

We load each prompt via the real `build_system_prompt` loader (which concatenates
preamble + call-type instructions + citations + grounding) and assert that the
key phrases from the two fixes are present.

These are text-presence checks, not behaviour checks — we are only protecting
against the rules being silently removed.
"""

from __future__ import annotations

import pytest

from rumil.llm import build_system_prompt

_HEADLINE_DISCIPLINE_CALL_TYPES = ("find_considerations", "assess", "draft_artifact")
_SOURCE_QUALITY_CALL_TYPES = ("web_research", "ingest")


@pytest.mark.parametrize("call_type", _HEADLINE_DISCIPLINE_CALL_TYPES)
def test_headline_discipline_rule_present(call_type: str) -> None:
    prompt = build_system_prompt(call_type)
    assert "Headline discipline" in prompt
    assert "no stronger than the weakest caveat" in prompt


@pytest.mark.parametrize("call_type", _SOURCE_QUALITY_CALL_TYPES)
def test_source_quality_preference_present(call_type: str) -> None:
    prompt = build_system_prompt(call_type)
    assert "Source quality preference" in prompt
    assert "primary literature" in prompt


def test_find_considerations_primary_source_oneliner_present() -> None:
    prompt = build_system_prompt("find_considerations")
    assert "strong primary source exists but you're citing a weak one" in prompt


def test_assess_inherits_hedges_on_supersede() -> None:
    prompt = build_system_prompt("assess")
    assert "inherit those hedges" in prompt


def test_draft_artifact_inherits_hedges() -> None:
    prompt = build_system_prompt("draft_artifact")
    assert "inherit its hedges" in prompt
