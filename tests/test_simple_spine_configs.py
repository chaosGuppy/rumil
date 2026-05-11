"""Smoke tests for the shipped SimpleSpine YAML configs + preset registry.

Loads every YAML in ``src/rumil/orchestrators/simple_spine/configs/``
through the real loader and checks the resulting ``SimpleSpineConfig``
is well-formed. Catches:

- field renames that miss a config (e.g. ``base_token_cap`` →
  ``base_cost_cap_usd``)
- token-era values left in a USD field after a units migration
  (the ``< 100`` USD ceiling)
- preset-registry drift (every YAML file is auto-registered;
  ``default`` aliases ``research``; YAML's ``name:`` matches stem)
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from rumil.orchestrators.simple_spine.loader import (
    discover_configs,
    load_simple_spine_config,
)
from rumil.orchestrators.simple_spine.presets import get_preset, list_presets

_CONFIGS_DIR = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "rumil"
    / "orchestrators"
    / "simple_spine"
    / "configs"
)
_SHIPPED_CONFIGS = discover_configs(_CONFIGS_DIR)


@pytest.mark.parametrize("config_path", _SHIPPED_CONFIGS, ids=lambda p: p.stem)
def test_shipped_config_loads_cleanly(config_path: Path):
    cfg = load_simple_spine_config(config_path)
    assert cfg.main_model
    assert cfg.process_library

    for sub in cfg.process_library:
        if hasattr(sub, "model"):
            assert isinstance(sub.model, str) and sub.model, (  # pyright: ignore[reportAttributeAccessIssue]
                f"{sub.name}.model must be a non-empty str"
            )
        if hasattr(sub, "cache"):
            assert isinstance(sub.cache, bool), (  # pyright: ignore[reportAttributeAccessIssue]
                f"{sub.name}.cache must be bool"
            )
        cap = getattr(sub, "base_cost_cap_usd", None)
        if cap is not None:
            assert isinstance(cap, (int, float))
            # USD ceilings are sub-$100 for any sane spawn; pre-refactor
            # token-era values lived in the 50_000-200_000 range, so
            # this check catches a missed migration on the next sed pass.
            assert 0 < cap < 100, (
                f"{sub.name}.base_cost_cap_usd={cap!r} looks like a "
                "token-era leftover, not a USD value"
            )


@pytest.mark.parametrize("config_path", _SHIPPED_CONFIGS, ids=lambda p: p.stem)
def test_shipped_config_name_matches_file_stem(config_path: Path):
    with config_path.open() as f:
        blob = yaml.safe_load(f)
    assert blob["name"] == config_path.stem, (
        f"YAML name={blob['name']!r} does not match file stem "
        f"{config_path.stem!r} — preset registry would expose it under "
        "the stem; rename one to match the other"
    )


def test_every_shipped_config_is_a_registered_preset():
    stems = {p.stem for p in _SHIPPED_CONFIGS}
    presets = set(list_presets())
    assert stems <= presets, f"missing from registry: {stems - presets}"
    # ``default`` is an alias auto-added on top of the file-stem set.
    assert "default" in presets


@pytest.mark.parametrize("config_path", _SHIPPED_CONFIGS, ids=lambda p: p.stem)
def test_get_preset_returns_same_fingerprint_as_yaml_load(config_path: Path):
    via_yaml = load_simple_spine_config(config_path)
    via_registry = get_preset(config_path.stem)
    assert via_registry.fingerprint == via_yaml.fingerprint


def test_default_preset_aliases_research():
    assert get_preset("default").fingerprint == get_preset("research").fingerprint
