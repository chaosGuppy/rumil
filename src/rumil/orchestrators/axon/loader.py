"""YAML loader for :class:`AxonConfig`.

Each variant lives in ``configs/<name>.yaml`` and references prompt
files relative to the config file. The loader reads the YAML, resolves
all paths, loads the system_prompt_registry entries, and returns a
fully-formed :class:`AxonConfig`. Tiny by design — axon's config
surface is much smaller than simple_spine's.

Schema:

```yaml
name: research
main_model: claude-opus-4-7
main_system_prompt_path: ../prompts/spine_main.md
max_parallel_delegates_per_turn: 4   # optional, default 4
hard_max_rounds: 50                  # optional, default 50
max_seed_pages: 20                   # optional, default 20
enable_server_compaction: true       # optional, default true
compaction_trigger_tokens: 400000    # optional, default 400_000
compaction_instructions_path: ../prompts/compaction_default.md  # optional
direct_tools: [load_page]            # tools mainline can call directly

system_prompt_registry:              # optional; configure references by name
  web_research: ../prompts/web_research_sys.md
  workspace_lookup: ../prompts/workspace_lookup_sys.md

finalize_schema_registry:            # optional; configure references by name
  freeform_text:
    type: object
    properties:
      answer: {type: string}
    required: [answer]
    additionalProperties: false
```
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from rumil.orchestrators.axon.artifacts import ArtifactSeed
from rumil.orchestrators.axon.config import AxonConfig

log = logging.getLogger(__name__)


def load_axon_config(path: str | Path) -> AxonConfig:
    """Load and resolve an axon YAML config from disk."""
    config_path = Path(path).resolve()
    if not config_path.exists():
        raise FileNotFoundError(f"axon config not found: {config_path}")
    with config_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"axon config {config_path} must be a YAML mapping at the top level")
    config_dir = config_path.parent

    name = _required_str(raw, "name", config_path)
    main_model = _required_str(raw, "main_model", config_path)
    main_system_prompt_path = _resolve_path(
        config_dir, _required_str(raw, "main_system_prompt_path", config_path)
    )

    direct_tools = tuple(raw.get("direct_tools", ["load_page"]) or ())

    artifact_seeds = _parse_artifact_seeds(
        raw.get("artifact_seeds") or {},
        config_dir,
        config_path,
    )

    finalize_schema_registry: dict[str, dict[str, Any]] = {}
    for schema_name, schema in (raw.get("finalize_schema_registry") or {}).items():
        if not isinstance(schema, dict):
            raise ValueError(
                f"finalize_schema_registry entry {schema_name!r} in {config_path} must be a mapping"
            )
        finalize_schema_registry[str(schema_name)] = schema

    compaction_path_raw = raw.get("compaction_instructions_path")
    compaction_path: Path | None = (
        _resolve_path(config_dir, str(compaction_path_raw)) if compaction_path_raw else None
    )

    return AxonConfig(
        name=name,
        main_model=main_model,
        main_system_prompt_path=main_system_prompt_path,
        max_parallel_delegates_per_turn=int(raw.get("max_parallel_delegates_per_turn", 4)),
        hard_max_rounds=int(raw.get("hard_max_rounds", 50)),
        max_seed_pages=int(raw.get("max_seed_pages", 20)),
        auto_seed_from_question=bool(raw.get("auto_seed_from_question", True)),
        auto_seed_match_threshold=float(raw.get("auto_seed_match_threshold", 0.5)),
        enable_server_compaction=bool(raw.get("enable_server_compaction", True)),
        compaction_trigger_tokens=int(raw.get("compaction_trigger_tokens", 400_000)),
        compaction_instructions_path=compaction_path,
        artifact_seeds=artifact_seeds,
        finalize_schema_registry=finalize_schema_registry,
        direct_tools=direct_tools,
        mainline_finalize_schema_ref=(
            str(raw["mainline_finalize_schema_ref"])
            if raw.get("mainline_finalize_schema_ref") is not None
            else None
        ),
    )


def _parse_artifact_seeds(
    raw_seeds: dict,
    config_dir: Path,
    config_path: Path,
) -> dict[str, ArtifactSeed]:
    """Parse the YAML ``artifact_seeds`` block into ArtifactSeed instances.

    Each entry is a mapping that supplies ``description`` plus exactly
    one of ``path`` (file path, resolved relative to the config dir) or
    ``text`` (inline body). ``render_inline`` defaults to False.
    """
    from rumil.orchestrators.axon.artifacts import ArtifactSeed

    out: dict[str, ArtifactSeed] = {}
    for key, raw_seed in raw_seeds.items():
        if not isinstance(raw_seed, dict):
            raise ValueError(
                f"artifact_seeds entry {key!r} in {config_path} must be a mapping "
                f"with `path` or `text` (got {type(raw_seed).__name__})"
            )
        path_val = raw_seed.get("path")
        text_val = raw_seed.get("text")
        if (path_val is None) == (text_val is None):
            raise ValueError(
                f"artifact_seeds entry {key!r} in {config_path} must set exactly "
                "one of `path` or `text`"
            )
        if path_val is not None:
            text = _resolve_path(config_dir, str(path_val)).read_text(encoding="utf-8")
        else:
            text = str(text_val)
        out[str(key)] = ArtifactSeed(
            text=text,
            description=str(raw_seed.get("description", "")),
            render_inline=bool(raw_seed.get("render_inline", False)),
        )
    return out


def discover_configs(configs_dir: str | Path | None = None) -> dict[str, Path]:
    """List config files in the given dir (defaults to axon/configs/)."""
    base = Path(configs_dir) if configs_dir else Path(__file__).parent / "configs"
    if not base.exists():
        return {}
    out: dict[str, Path] = {}
    for p in sorted(base.glob("*.yaml")):
        out[p.stem] = p
    return out


def _required_str(raw: dict, key: str, path: Path) -> str:
    val = raw.get(key)
    if not isinstance(val, str) or not val:
        raise ValueError(f"axon config {path}: missing or non-string `{key}`")
    return val


def _resolve_path(config_dir: Path, rel: str) -> Path:
    p = Path(rel)
    if p.is_absolute():
        return p
    return (config_dir / p).resolve()
