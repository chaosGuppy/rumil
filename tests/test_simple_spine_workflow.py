"""Tests for SimpleSpineWorkflow fingerprint + YAML loader artifact wiring.

Pin the post-artifact-channel contract:

- ``SimpleSpineWorkflow.fingerprint()`` includes ``artifacts_hash``.
- Same artifacts → same hash; different content (or different keys) →
  different hash. Per-pair pair_text / rubric edits naturally fork the
  versus dedup hash this way.
- The YAML loader parses ``consumes: [k1, k2]`` into the SubroutineBase
  field; non-list / non-string entries raise ValueError. Missing key
  defaults to empty tuple (the existing presets without consumes still
  load).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rumil.orchestrators.simple_spine.config import SimpleSpineConfig
from rumil.orchestrators.simple_spine.loader import load_simple_spine_config
from rumil.orchestrators.simple_spine.subroutines import (
    FreeformAgentSubroutine,
    SampleNSubroutine,
    SubroutineDef,
)
from rumil.orchestrators.simple_spine.workflow import SimpleSpineWorkflow


def _fake_config() -> SimpleSpineConfig:
    """A trivial SimpleSpineConfig usable as the workflow's preset.

    Uses a no-consumes FreeformAgent so the workflow can be constructed
    without touching the registered YAML presets. List-then-tuple shape
    mirrors the loader's typed pattern so pyright widens to SubroutineDef.
    """
    sub = FreeformAgentSubroutine(
        name="echo",
        description="d",
        sys_prompt="SYS",
        user_prompt_template="user {intent}",
        model="claude-haiku-4-5",
    )
    # Frozen-dataclass attrs vs writable Protocol attrs — pyright is
    # strict about the variance even though runtime checks pass. Same
    # workaround the loader uses (`# type: ignore[return-value]`).
    library: tuple[SubroutineDef, ...] = (sub,)  # pyright: ignore[reportAssignmentType]
    return SimpleSpineConfig(
        main_model="claude-haiku-4-5",
        process_library=library,
        main_system_prompt="MAIN",
    )


def _wf(*, artifacts: dict[str, str] | None = None) -> SimpleSpineWorkflow:
    # Construct with a registered preset name; immediately replace the
    # config attr with the fake one so the fingerprint test doesn't depend
    # on the real preset's contents.
    wf = SimpleSpineWorkflow(
        budget_tokens=40_000, config_name="research", call_type="judge", artifacts=artifacts
    )
    object.__setattr__(wf, "config", _fake_config())
    return wf


def test_fingerprint_includes_artifacts_hash():
    fp = _wf(artifacts={"pair_text": "abc"}).fingerprint()
    assert "artifacts_hash" in fp


def test_fingerprint_same_artifacts_same_hash():
    a = _wf(artifacts={"pair_text": "abc", "rubric": "xyz"}).fingerprint()
    b = _wf(artifacts={"pair_text": "abc", "rubric": "xyz"}).fingerprint()
    assert a["artifacts_hash"] == b["artifacts_hash"]


def test_fingerprint_forks_when_artifact_content_changes():
    a = _wf(artifacts={"pair_text": "abc"}).fingerprint()
    b = _wf(artifacts={"pair_text": "DIFFERENT"}).fingerprint()
    assert a["artifacts_hash"] != b["artifacts_hash"]


def test_fingerprint_forks_when_artifact_key_changes():
    a = _wf(artifacts={"pair_text": "abc"}).fingerprint()
    b = _wf(artifacts={"different_key": "abc"}).fingerprint()
    assert a["artifacts_hash"] != b["artifacts_hash"]


def test_fingerprint_no_artifacts_stable():
    """Empty artifacts dict produces a deterministic hash; same shape both runs."""
    a = _wf().fingerprint()
    b = _wf().fingerprint()
    assert a["artifacts_hash"] == b["artifacts_hash"]


def test_fingerprint_artifact_order_independent():
    """The hash sorts by key before hashing, so insertion order doesn't matter."""
    a = _wf(artifacts={"a": "1", "b": "2"}).fingerprint()
    b = _wf(artifacts={"b": "2", "a": "1"}).fingerprint()
    assert a["artifacts_hash"] == b["artifacts_hash"]


def test_constructor_accepts_artifacts_kwarg():
    wf = SimpleSpineWorkflow(
        budget_tokens=40_000,
        config_name="research",
        call_type="judge",
        artifacts={"foo": "bar"},
    )
    assert wf.artifacts == {"foo": "bar"}


def test_constructor_default_artifacts_empty():
    wf = SimpleSpineWorkflow(budget_tokens=40_000, config_name="research", call_type="judge")
    assert wf.artifacts == {}


# YAML loader contract: consumes parses, non-string entries raise.

_MINIMAL_YAML = """
name: t-{name}
main_model: claude-haiku-4-5
main_system_prompt: |
  inline sys prompt
subroutines:
  - kind: freeform_agent
    name: echo
    sys_prompt: |
      sys
    user_prompt_template: |
      user {{intent}}
    model: claude-haiku-4-5
    {extra}
"""


def _write_yaml(tmp_path: Path, name: str, extra: str = "") -> Path:
    text = _MINIMAL_YAML.format(name=name, extra=extra)
    path = tmp_path / f"{name}.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_yaml_loader_parses_consumes(tmp_path: Path):
    path = _write_yaml(tmp_path, "consumes-set", extra="consumes: [pair_text, rubric]")
    cfg = load_simple_spine_config(path)
    sub = cfg.process_library[0]
    assert sub.consumes == ("pair_text", "rubric")


def test_yaml_loader_consumes_default_empty(tmp_path: Path):
    path = _write_yaml(tmp_path, "consumes-absent")
    cfg = load_simple_spine_config(path)
    sub = cfg.process_library[0]
    assert sub.consumes == ()


def test_yaml_loader_consumes_rejects_non_string_entries(tmp_path: Path):
    path = _write_yaml(tmp_path, "consumes-bad", extra="consumes: [pair_text, 123]")
    with pytest.raises(ValueError, match="consumes"):
        load_simple_spine_config(path)


def test_yaml_loader_consumes_rejects_non_list(tmp_path: Path):
    path = _write_yaml(tmp_path, "consumes-scalar", extra='consumes: "pair_text"')
    with pytest.raises(ValueError, match="consumes"):
        load_simple_spine_config(path)


def test_judge_pair_preset_loads_with_consumes():
    """Each judge_pair subroutine declares consumes=[pair_text, rubric] —
    pin this so a future YAML edit that drops them breaks the test
    rather than silently producing thin-context judgments again.
    """
    from rumil.orchestrators.simple_spine.presets import get_preset

    cfg = get_preset("judge_pair")
    for sub in cfg.process_library:
        assert "pair_text" in sub.consumes
        assert "rubric" in sub.consumes


def test_sample_n_loads_with_consumes(tmp_path: Path):
    """SampleN should also accept consumes via the YAML loader (used by
    the steelman subroutine in judge_pair).
    """
    text = (
        "name: t\nmain_model: claude-haiku-4-5\nmain_system_prompt: |\n  s\n"
        "subroutines:\n  - kind: sample_n\n    name: s\n"
        "    sys_prompt: |\n      sys\n"
        "    user_prompt_template: |\n      user {intent}\n"
        "    model: claude-haiku-4-5\n    n: 2\n"
        "    consumes: [pair_text]\n"
    )
    path = tmp_path / "sn.yaml"
    path.write_text(text, encoding="utf-8")
    cfg = load_simple_spine_config(path)
    sub = cfg.process_library[0]
    assert isinstance(sub, SampleNSubroutine)
    assert sub.consumes == ("pair_text",)
