"""Pin invariants of the structured-judge-config layer.

Covers ``versus.judge_config.make_judge_config`` (single compose site),
``compute_config_hash`` (canonical-JSON determinism), the per-variant
required-field assertions, the display ``judge_model`` shape, the
fingerprint helper's content-sensitivity, and the provenance reader's
config-driven axis projection.

Pure-Python tests: no LLM, no DB, no network. Fast.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

_VERSUS_SRC = Path(__file__).resolve().parents[1] / "versus" / "src"
if str(_VERSUS_SRC) not in sys.path:
    sys.path.insert(0, str(_VERSUS_SRC))

from versus.judge_config import (  # noqa: E402
    compute_config_hash,
    compute_file_fingerprint,
    make_judge_config,
    project_config_to_axes,
)

from versus import mainline as versus_mainline  # noqa: E402

_BLIND_KW: dict[str, Any] = {
    "model": "claude-opus-4-7",
    "dimension": "general_quality",
    "sampling": {"temperature": None, "max_tokens": 1024},
    "prompt_hash": "deadbeef",
    "thinking": None,
}

_WS_KW: dict[str, Any] = {
    **_BLIND_KW,
    "tool_prompt_hash": "11111111",
    "pair_surface_hash": "22222222",
    "workspace_id": "abcd1234",
    "code_fingerprint": {"src/rumil/versus_bridge.py": "aaaaaaaa"},
    "workspace_state_hash": "0011223344556677",
}

_ORCH_KW: dict[str, Any] = {
    **_WS_KW,
    "budget": 4,
    "closer_hash": "33333333",
}


def test_config_hash_is_deterministic_for_identical_inputs():
    cfg_a, hash_a, _ = make_judge_config("blind", **_BLIND_KW)
    cfg_b, hash_b, _ = make_judge_config("blind", **_BLIND_KW)
    assert cfg_a == cfg_b
    assert hash_a == hash_b


@pytest.mark.parametrize(
    ("override_key", "override_value"),
    (
        ("model", "claude-sonnet-4-6"),
        ("dimension", "grounding"),
        ("sampling", {"temperature": 0.2, "max_tokens": 1024}),
        ("prompt_hash", "abcd1234"),
        ("thinking", {"type": "adaptive"}),
    ),
)
def test_config_hash_changes_when_any_input_changes(override_key, override_value):
    _, base_hash, _ = make_judge_config("blind", **_BLIND_KW)
    bumped_kw = {**_BLIND_KW, override_key: override_value}
    _, bumped_hash, _ = make_judge_config("blind", **bumped_kw)
    assert base_hash != bumped_hash


def test_thinking_field_recorded_when_non_none():
    kw = {**_BLIND_KW, "thinking": {"type": "adaptive", "display": "summarized"}}
    cfg, _, _ = make_judge_config("blind", **kw)
    assert cfg["thinking"] == {"type": "adaptive", "display": "summarized"}


def test_thinking_field_present_as_none_when_omitted():
    cfg, _, _ = make_judge_config("blind", **_BLIND_KW)
    assert "thinking" in cfg
    assert cfg["thinking"] is None


def test_orch_config_hash_changes_when_code_fingerprint_changes():
    _, base_hash, _ = make_judge_config("orch", **_ORCH_KW)
    bumped = {
        **_ORCH_KW,
        "code_fingerprint": {"src/rumil/versus_bridge.py": "ffffffff"},
    }
    _, bumped_hash, _ = make_judge_config("orch", **bumped)
    assert base_hash != bumped_hash


@pytest.mark.parametrize(
    ("variant", "missing"),
    (
        ("ws", "tool_prompt_hash"),
        ("ws", "pair_surface_hash"),
        ("ws", "workspace_id"),
        ("ws", "code_fingerprint"),
        ("ws", "workspace_state_hash"),
        ("orch", "budget"),
        ("orch", "closer_hash"),
        ("orch", "code_fingerprint"),
        ("orch", "workspace_state_hash"),
    ),
)
def test_missing_required_arg_raises_value_error(variant, missing):
    kw = dict(_ORCH_KW if variant == "orch" else _WS_KW)
    kw[missing] = None
    with pytest.raises(ValueError):
        make_judge_config(variant, **kw)


def test_blind_judge_model_display_shape():
    _, ch, jm = make_judge_config("blind", **_BLIND_KW)
    # New display shape: blind:<model>:<dim>:c<hash8>
    assert jm == f"blind:claude-opus-4-7:general_quality:c{ch[:8]}"


def test_ws_judge_model_display_shape():
    _, ch, jm = make_judge_config("ws", **_WS_KW)
    assert jm == f"rumil:ws:claude-opus-4-7:general_quality:c{ch[:8]}"


def test_orch_judge_model_display_shape():
    _, ch, jm = make_judge_config("orch", **_ORCH_KW)
    assert jm == f"rumil:orch:claude-opus-4-7:general_quality:c{ch[:8]}"


def test_compute_file_fingerprint_picks_up_content_changes(tmp_path, monkeypatch):
    # compute_file_fingerprint anchors at the rumil repo root via its
    # own helper. Patch the helper to point at tmp_path so we can test
    # against files we control.
    from versus import judge_config as jc

    monkeypatch.setattr(jc, "_repo_root", lambda: tmp_path)
    target = tmp_path / "thing.txt"
    target.write_text("alpha")
    fp_a = compute_file_fingerprint(["thing.txt"])
    target.write_text("beta")
    fp_b = compute_file_fingerprint(["thing.txt"])
    assert fp_a["thing.txt"] != fp_b["thing.txt"]


def test_compute_file_fingerprint_records_missing_paths_as_empty(tmp_path, monkeypatch):
    from versus import judge_config as jc

    monkeypatch.setattr(jc, "_repo_root", lambda: tmp_path)
    fp = compute_file_fingerprint(["nope.txt"])
    assert fp == {"nope.txt": ""}


def test_summarize_provenance_reads_from_config_when_present():
    cfg, ch, jm = make_judge_config("ws", **_WS_KW)
    rows = [
        {"prefix_config_hash": "ph1", "judge_model": jm, "config": cfg, "config_hash": ch},
    ]
    out = versus_mainline.summarize_provenance(rows)
    assert out["judge_path"] == {"rumil:ws": 1}
    assert out["judge_workspace_id"] == {"abcd1234": 1}
    assert out["judge_pair_hash"] == {"q22222222": 1}


@pytest.mark.parametrize(
    ("variant", "kw", "expected_keys"),
    (
        ("blind", _BLIND_KW, {"variant", "model", "task", "phash"}),
        ("ws", _WS_KW, {"variant", "model", "task", "phash"}),
        ("orch", _ORCH_KW, {"variant", "model", "task", "phash"}),
    ),
)
def test_label_from_config_shape(variant, kw, expected_keys):
    """``analyze.label_from_config`` returns the four-field header dict
    the FE expects (``variant`` / ``model`` / ``task`` / ``phash``)
    across every variant.
    """
    from versus import analyze as versus_analyze

    cfg, _, _ = make_judge_config(variant, **kw)
    out = versus_analyze.label_from_config(cfg)
    assert set(out.keys()) == expected_keys


# Config field → axis name. Pins the projection contract: a new
# config field must add an entry here AND a corresponding axis in
# ``project_config_to_axes``. Missing either raises the test below.
_CONFIG_FIELD_TO_AXIS = {
    "model": "judge_base_model",
    "dimension": "judge_dimension",
    "workspace_id": "judge_workspace_id",
    "tool_descriptions_hash": "judge_tool_hash",
    "pair_surface_hash": "judge_pair_hash",
    "code_fingerprint": "judge_code_fingerprint",
    "workspace_state_hash": "judge_workspace_state_hash",
    "budget": "judge_budget",
    "closer_hash": "judge_closer_hash",
}
# Top-level keys deliberately not exposed as their own axis (covered
# by other axes implicitly). ``thinking`` projects only when non-None,
# matching ``sampling`` — both are folded into config_hash but only
# surface as a separate axis when set.
_CONFIG_FIELDS_OPAQUE = {"variant", "sampling", "prompts", "thinking"}


@pytest.mark.parametrize(
    ("variant", "kw"),
    (
        ("blind", _BLIND_KW),
        ("ws", _WS_KW),
        ("orch", _ORCH_KW),
    ),
)
def test_project_config_to_axes_covers_every_config_field(variant, kw):
    """Pins config_hash's role as a completeness check: every config
    field must either be opaque-by-design or have an entry in
    ``_CONFIG_FIELD_TO_AXIS`` that points at a real axis. Adding a
    new field to ``make_judge_config`` without updating the mapping
    fails this test — preventing silent drift where ``config_hash``
    would diverge from the per-axis panel.
    """
    cfg, ch, _ = make_judge_config(variant, **kw)
    axes = project_config_to_axes(cfg, config_hash=ch)
    for key in cfg:
        if key in _CONFIG_FIELDS_OPAQUE:
            continue
        axis = _CONFIG_FIELD_TO_AXIS.get(key)
        assert axis is not None, (
            f"config field {key!r} has no entry in _CONFIG_FIELD_TO_AXIS — "
            "either add a corresponding axis to project_config_to_axes "
            "or mark it opaque."
        )
        assert axis in axes, (
            f"config field {key!r} maps to axis {axis!r} but that axis "
            f"isn't produced by project_config_to_axes (got {sorted(axes)})"
        )


def test_compute_config_hash_is_canonical_across_key_order():
    a = compute_config_hash({"x": 1, "y": [2, 3], "z": {"q": True}})
    b = compute_config_hash({"z": {"q": True}, "y": [2, 3], "x": 1})
    assert a == b
