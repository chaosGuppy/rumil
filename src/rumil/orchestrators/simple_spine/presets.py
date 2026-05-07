"""Named SimpleSpineConfig presets — registry + auto-discovery.

Variants live in ``configs/<name>.yaml`` and are auto-registered at
module import time via :func:`load_simple_spine_config`. Registered
names are usable from CLI as ``--workflow-arg config_name=<name>``.

Built-in variants currently shipped under ``configs/``:

- ``essay_continuation`` — drafter (FreeformAgent, 32k output) + critic
  (SampleN n=3). Used by ``--orch simple_spine`` on the completion side.
- ``judge_pair`` — read → steelman (SampleN n=2) → verdict (FreeformAgent
  with response_validator + retry on non-canonical label). Used by
  ``--variant simple_spine`` on the judging side. Mainline system
  prompt extends the default with the 7-point wire-format constraint.

Adding a new variant: drop ``configs/<name>.yaml`` referencing prompts
in ``prompts/``. No code edits needed unless the variant uses a new
``response_validator`` (register in ``validators.py``) or an
unsupported subroutine kind (extend ``loader.py``).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from rumil.orchestrators.simple_spine.config import SimpleSpineConfig
from rumil.orchestrators.simple_spine.loader import (
    discover_configs,
    load_simple_spine_config,
)

PresetBuilder = Callable[[], SimpleSpineConfig]
_REGISTRY: dict[str, PresetBuilder] = {}


def register_preset(name: str, builder: PresetBuilder) -> None:
    """Register a SimpleSpineConfig builder under ``name``.

    Idempotent: re-registering the same name silently overwrites — this
    keeps test fixtures and module-level register calls safe to re-run.
    """
    _REGISTRY[name] = builder


def get_preset(name: str) -> SimpleSpineConfig:
    """Look up and instantiate a SimpleSpineConfig by name."""
    if name not in _REGISTRY:
        known = sorted(_REGISTRY)
        raise KeyError(f"unknown SimpleSpine preset {name!r}; registered: {known}")
    return _REGISTRY[name]()


def list_presets() -> list[str]:
    return sorted(_REGISTRY)


def _yaml_loader(path: Path) -> PresetBuilder:
    """Defer YAML loading until first ``get_preset`` call.

    We avoid loading at import time so prompt-file edits land without
    a Python restart; only the first call to ``get_preset(name)`` per
    process pays the YAML+prompt-file read.
    """

    def _builder() -> SimpleSpineConfig:
        return load_simple_spine_config(path)

    return _builder


def _autoregister_yaml_configs() -> None:
    configs_dir = Path(__file__).parent / "configs"
    for yaml_path in discover_configs(configs_dir):
        # Use the file stem as the registered name; the YAML's `name`
        # field is required to match for clarity but not enforced here
        # (the loader validates the name field separately).
        _REGISTRY[yaml_path.stem] = _yaml_loader(yaml_path)
    # ``default`` aliases ``essay_continuation`` for back-compat with
    # callers that don't pass a config_name.
    if "essay_continuation" in _REGISTRY:
        _REGISTRY["default"] = _REGISTRY["essay_continuation"]


_autoregister_yaml_configs()
