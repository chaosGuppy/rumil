"""Pin ``versus.analyze.model_sort_key`` across every judge_model shape.

Three shapes coexist in versus rows: legacy compound
(``rumil:orch:...`` / ``blind:...``), new compound
(``judge_pair/<workflow>:<model>:c<hash8>``, post-#424), and bare
provider-prefixed model ids (``openai/gpt-5``). The sort key feeds the
column ordering on the inspect / results UIs — the new shape was
falling into the ``provider/model`` branch and parsing as
``base="two_phase:claude-opus-4-7:c..."``, sorting nonsensically.
"""

from __future__ import annotations

import sys
from pathlib import Path

_VERSUS_SRC = Path(__file__).resolve().parents[1] / "versus" / "src"
if str(_VERSUS_SRC) not in sys.path:
    sys.path.insert(0, str(_VERSUS_SRC))

from versus.analyze import model_sort_key  # noqa: E402


def _variant(judge: str) -> int:
    # The sort tuple is (family, strength, base_low, variant, judge); the
    # variant is at index 3.
    return model_sort_key(judge)[3]


def _base(judge: str) -> str:
    return model_sort_key(judge)[2]


def test_legacy_orch_keeps_priority_3():
    judge = "rumil:orch:claude-opus-4-7:general_quality:cabcdef12"
    assert _variant(judge) == 3
    assert _base(judge) == "claude-opus-4-7"


def test_legacy_ws_keeps_priority_2():
    judge = "rumil:ws:claude-opus-4-7:general_quality:cabcdef12"
    assert _variant(judge) == 2
    assert _base(judge) == "claude-opus-4-7"


def test_legacy_blind_keeps_priority_1():
    judge = "blind:claude-opus-4-7:general_quality:cabcdef12"
    assert _variant(judge) == 1
    assert _base(judge) == "claude-opus-4-7"


def test_new_blind_matches_legacy_blind_priority():
    judge = "judge_pair/blind:claude-opus-4-7:cabcdef12"
    assert _variant(judge) == 1
    assert _base(judge) == "claude-opus-4-7"


def test_new_two_phase_matches_legacy_orch_priority():
    judge = "judge_pair/two_phase:claude-opus-4-7:cabcdef12"
    assert _variant(judge) == 3
    assert _base(judge) == "claude-opus-4-7"


def test_new_draft_and_edit_matches_legacy_orch_priority():
    # Forward-looking: draft_and_edit hasn't shipped yet but should sort
    # alongside two_phase as a rumil-produced workflow.
    judge = "judge_pair/draft_and_edit:claude-opus-4-7:cabcdef12"
    assert _variant(judge) == 3
    assert _base(judge) == "claude-opus-4-7"


def test_provider_model_unchanged():
    # A bare provider/model id — no leading task prefix, no colon —
    # must keep falling through to the generic ``"/" in judge`` branch
    # and parse as ``base="gpt-5.4"`` with variant=0.
    judge = "openai/gpt-5.4"
    assert _variant(judge) == 0
    assert _base(judge) == "gpt-5.4"


def test_paraphrase_provider_model_unchanged():
    # ``paraphrase:openai/gpt-5`` — the leading ``paraphrase:`` segment
    # has no ``/``, so the new-shape branch does not match; falls
    # through to the generic ``"/" in judge`` branch.
    judge = "paraphrase:openai/gpt-5"
    assert _variant(judge) == 0


def test_human_unchanged():
    judge = "human"
    key = model_sort_key(judge)
    assert key[3] == 0
    assert key[2] == "human"


def test_new_shape_anthropic_family_resolves():
    # The family/strength axes (the rest of the tuple) must still
    # resolve correctly off the extracted base model — opus → family=2,
    # strength=2 — not off the full judge_model string.
    judge = "judge_pair/two_phase:claude-opus-4-7:cabcdef12"
    family, strength, base, _, _ = model_sort_key(judge)
    assert (family, strength) == (2, 2)
    assert base == "claude-opus-4-7"


def test_new_shape_haiku_sorts_below_opus():
    haiku = "judge_pair/two_phase:claude-haiku-4-5:cabcdef12"
    opus = "judge_pair/two_phase:claude-opus-4-7:cabcdef12"
    assert model_sort_key(haiku) < model_sort_key(opus)


def test_new_shape_blind_sorts_below_two_phase_at_same_model():
    blind = "judge_pair/blind:claude-opus-4-7:cabcdef12"
    two_phase = "judge_pair/two_phase:claude-opus-4-7:cabcdef12"
    assert model_sort_key(blind) < model_sort_key(two_phase)
