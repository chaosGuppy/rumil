"""YAML loader for SimpleSpineConfig variants.

Each variant lives in ``configs/<name>.yaml`` and references prompt
files (under ``prompts/``) by path relative to the config file. The
loader resolves all paths, looks up runtime-only objects through small
registries (validators, runner classes, orch factories), and returns a
fully-formed :class:`SimpleSpineConfig`.

Schema (subroutine kinds use ``kind`` discriminator):

```yaml
name: <variant-name>            # registered as the preset name
main_model: <model-id>
main_system_prompt_path: prompts/main_default.md
main_system_prompt: |           # OR inline; one or the other
  ...
main_system_prompt_extra_path: prompts/foo_extra.md  # optional appendix
main_system_prompt_extra: |     # OR inline; appended to the base
  ...
max_parallel_spawns_per_turn: 4 # optional
enable_finalize_tool: true      # optional, default true

subroutines:
  - kind: freeform_agent
    name: draft
    description: ...
    sys_prompt_path: prompts/drafter_essay_sys.md   # OR sys_prompt: |
    user_prompt_template: |     # inline (multiline ok)
      ## Intent
      {intent}
      ...
    model: claude-opus-4-7
    max_rounds: 1
    max_tokens: 32000
    overridable: [intent, additional_context]   # optional
    inherit_assumptions: true   # optional, default true; opt out for roles
                                 # whose job is to challenge framings
    response_validator: extract_preference_not_none   # optional, registry key
    retry_message_path: prompts/verdict_judge_retry.md  # required if validator set
    response_max_retries: 2     # optional, default 1
    base_token_cap: 8000        # optional; enables `token_cap` override on
                                 # the spawn schema (when `token_cap` is in
                                 # `overridable`). Carved via carve_child so
                                 # the spawn cannot exceed this without
                                 # adding extra budget.
    cost_hint: "≈ 1 opus turn @ 32k out"  # optional; appended to the spawn
                                 # tool description so mainline can plan its
                                 # first spawn before live cost feedback.
    intent_description: ...     # optional; per-subroutine override for the
                                 # generic kind-level `intent` field schema
                                 # description. Use to give role-specific
                                 # framing (e.g. "A side label, 'A' or 'B'").
    additional_context_description: ...  # optional; same idea for
                                          # `additional_context`.

  - kind: sample_n
    name: critique
    description: ...
    sys_prompt_path: ...
    user_prompt_template: |
      ...
    model: claude-sonnet-4-6
    n: 3
    temperature: 1.0
    max_tokens: 2048
    overridable: [intent, n]
    inherit_assumptions: false  # optional, default true
    # base_token_cap, cost_hint, intent_description,
    # additional_context_description — same as freeform_agent above.
```

CallTypeSubroutine and NestedOrchSubroutine kinds aren't supported in
YAML yet — they reference Python classes / factories that need a richer
registry. Add when needed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from rumil.orchestrators.simple_spine.config import SimpleSpineConfig
from rumil.orchestrators.simple_spine.nested_orchs import get_orch_factory
from rumil.orchestrators.simple_spine.runners import get_call_type
from rumil.orchestrators.simple_spine.subroutines import (
    CallTypeSubroutine,
    FreeformAgentSubroutine,
    NestedOrchSubroutine,
    SampleNSubroutine,
    SubroutineDef,
)
from rumil.orchestrators.simple_spine.validators import get_validator


def load_simple_spine_config(path: str | Path) -> SimpleSpineConfig:
    """Load a SimpleSpineConfig from a YAML file.

    All path-references in the YAML are resolved relative to the YAML
    file's directory, so configs are self-contained — moving a variant
    to a new location only requires moving its prompts alongside it.

    Raises :class:`KeyError` for unknown subroutine kinds, validator
    names, or referenced prompt files. Raises :class:`ValueError` for
    schema violations (missing required fields, mutually-exclusive
    fields both set).
    """
    path = Path(path).resolve()
    with path.open("r", encoding="utf-8") as f:
        blob = yaml.safe_load(f)
    if not isinstance(blob, dict):
        raise ValueError(f"{path}: top-level must be a mapping, got {type(blob).__name__}")

    base_dir = path.parent
    main_system_prompt = _resolve_prompt(
        blob, "main_system_prompt", "main_system_prompt_path", base_dir
    )
    if main_system_prompt is None:
        raise ValueError(f"{path}: must set main_system_prompt or main_system_prompt_path")
    extra = _resolve_prompt(
        blob, "main_system_prompt_extra", "main_system_prompt_extra_path", base_dir
    )
    if extra is not None:
        main_system_prompt = main_system_prompt.rstrip() + "\n\n" + extra.lstrip()

    name = blob.get("name")
    if not name or not isinstance(name, str):
        raise ValueError(f"{path}: must set top-level `name` (preset key)")
    main_model = blob.get("main_model")
    if not main_model or not isinstance(main_model, str):
        raise ValueError(f"{path}: must set top-level `main_model`")

    raw_subs = blob.get("subroutines") or []
    if not isinstance(raw_subs, list):
        raise ValueError(f"{path}: `subroutines` must be a list")
    library: list[SubroutineDef] = [
        _load_subroutine(entry, base_dir, source=path) for entry in raw_subs
    ]

    cfg_kwargs: dict[str, Any] = {
        "main_model": main_model,
        "main_system_prompt": main_system_prompt,
        "process_library": tuple(library),
    }
    if "max_parallel_spawns_per_turn" in blob:
        cfg_kwargs["max_parallel_spawns_per_turn"] = blob["max_parallel_spawns_per_turn"]
    if "enable_finalize_tool" in blob:
        cfg_kwargs["enable_finalize_tool"] = bool(blob["enable_finalize_tool"])
    if "force_finalize_on_token_exhaustion" in blob:
        cfg_kwargs["force_finalize_on_token_exhaustion"] = bool(
            blob["force_finalize_on_token_exhaustion"]
        )
    return SimpleSpineConfig(**cfg_kwargs)


def _resolve_prompt(
    entry: dict[str, Any], inline_key: str, path_key: str, base_dir: Path
) -> str | None:
    """Resolve an inline-or-path prompt field; both-set is an error."""
    inline = entry.get(inline_key)
    pth = entry.get(path_key)
    if inline is not None and pth is not None:
        raise ValueError(f"{base_dir}: {inline_key!r} and {path_key!r} are mutually exclusive")
    if inline is not None:
        if not isinstance(inline, str) or not inline.strip():
            raise ValueError(f"{base_dir}: {inline_key!r} must be a non-empty string")
        return inline.rstrip() + "\n"
    if pth is not None:
        if not isinstance(pth, str):
            raise ValueError(f"{base_dir}: {path_key!r} must be a string path")
        full = (base_dir / pth).resolve()
        text = full.read_text(encoding="utf-8")
        if not text.strip():
            raise ValueError(f"{full}: prompt file is empty or whitespace-only")
        return text
    return None


def _load_subroutine(entry: dict[str, Any], base_dir: Path, *, source: Path) -> SubroutineDef:
    if not isinstance(entry, dict):
        raise ValueError(f"{source}: each subroutine must be a mapping")
    kind = entry.get("kind")
    if not kind:
        raise ValueError(f"{source}: subroutine missing `kind`")
    name = entry.get("name") or "<unnamed>"
    if kind == "freeform_agent":
        return _load_freeform_agent(entry, base_dir, source)  # type: ignore[return-value]
    if kind == "sample_n":
        return _load_sample_n(entry, base_dir, source)  # type: ignore[return-value]
    if kind == "call_type":
        return _load_call_type(entry, source)  # type: ignore[return-value]
    if kind == "nested_orch":
        return _load_nested_orch(entry, source)  # type: ignore[return-value]
    raise ValueError(
        f"{source}: subroutine {name!r} has unknown kind {kind!r}; "
        "supported kinds: freeform_agent, sample_n, call_type, nested_orch"
    )


def _base_field_kwargs(entry: dict[str, Any]) -> dict[str, Any]:
    """Pull SubroutineBase fields off the YAML entry into kwargs.

    Single source for loading the cross-cutting fields shared by every
    subroutine kind (overridable, cost_hint, intent_description,
    additional_context_description, inherit_assumptions, base_token_cap).
    Used by every kind's loader so YAML field handling is uniform —
    adding a field to SubroutineBase requires editing only this helper.
    """
    out: dict[str, Any] = {}
    if "overridable" in entry:
        out["overridable"] = frozenset(entry["overridable"])
    if "inherit_assumptions" in entry:
        out["inherit_assumptions"] = bool(entry["inherit_assumptions"])
    if "base_token_cap" in entry:
        out["base_token_cap"] = int(entry["base_token_cap"])
    if "cost_hint" in entry:
        out["cost_hint"] = str(entry["cost_hint"])
    if "intent_description" in entry:
        out["intent_description"] = str(entry["intent_description"])
    if "additional_context_description" in entry:
        out["additional_context_description"] = str(entry["additional_context_description"])
    return out


def _load_freeform_agent(
    entry: dict[str, Any], base_dir: Path, source: Path
) -> FreeformAgentSubroutine:
    sys_prompt = _resolve_prompt(entry, "sys_prompt", "sys_prompt_path", base_dir)
    if sys_prompt is None:
        raise ValueError(
            f"{source}: freeform_agent {entry.get('name')!r} must set sys_prompt or sys_prompt_path"
        )
    user_template = _resolve_prompt(
        entry, "user_prompt_template", "user_prompt_template_path", base_dir
    )
    if user_template is None:
        raise ValueError(
            f"{source}: freeform_agent {entry.get('name')!r} must set user_prompt_template"
        )
    kwargs: dict[str, Any] = {
        "name": entry["name"],
        "description": entry.get("description", ""),
        "sys_prompt": sys_prompt,
        "user_prompt_template": user_template,
        "model": entry["model"],
        "max_rounds": int(entry.get("max_rounds", 5)),
        "max_tokens": int(entry.get("max_tokens", 4096)),
        "allowed_tool_names": tuple(entry.get("allowed_tool_names") or ()),
        **_base_field_kwargs(entry),
    }

    validator_name = entry.get("response_validator")
    if validator_name:
        validator = get_validator(validator_name)
        retry_message = _resolve_prompt(entry, "retry_message", "retry_message_path", base_dir)
        if retry_message is None:
            raise ValueError(
                f"{source}: freeform_agent {entry.get('name')!r} sets response_validator "
                "but no retry_message / retry_message_path"
            )
        kwargs["response_validator"] = validator
        kwargs["response_validator_name"] = validator_name
        kwargs["retry_message"] = retry_message
        if "response_max_retries" in entry:
            kwargs["response_max_retries"] = int(entry["response_max_retries"])
    return FreeformAgentSubroutine(**kwargs)  # type: ignore[return-value]


def _load_sample_n(entry: dict[str, Any], base_dir: Path, source: Path) -> SampleNSubroutine:
    sys_prompt = _resolve_prompt(entry, "sys_prompt", "sys_prompt_path", base_dir)
    if sys_prompt is None:
        raise ValueError(
            f"{source}: sample_n {entry.get('name')!r} must set sys_prompt or sys_prompt_path"
        )
    user_template = _resolve_prompt(
        entry, "user_prompt_template", "user_prompt_template_path", base_dir
    )
    if user_template is None:
        raise ValueError(f"{source}: sample_n {entry.get('name')!r} must set user_prompt_template")
    kwargs: dict[str, Any] = {
        "name": entry["name"],
        "description": entry.get("description", ""),
        "sys_prompt": sys_prompt,
        "user_prompt_template": user_template,
        "model": entry["model"],
        "n": int(entry.get("n", 3)),
        "temperature": float(entry.get("temperature", 1.0)),
        "max_tokens": int(entry.get("max_tokens", 4096)),
        **_base_field_kwargs(entry),
    }
    return SampleNSubroutine(**kwargs)  # type: ignore[return-value]


def _load_call_type(entry: dict[str, Any], source: Path) -> CallTypeSubroutine:
    key = entry.get("call_type_key")
    if not key or not isinstance(key, str):
        raise ValueError(
            f"{source}: call_type subroutine {entry.get('name')!r} requires "
            "`call_type_key` (string registry key in runners.py)"
        )
    call_type, runner_cls = get_call_type(key)
    kwargs: dict[str, Any] = {
        "name": entry["name"],
        "description": entry.get("description", ""),
        "call_type": call_type,
        "runner_cls": runner_cls,
        "base_max_rounds": int(entry.get("base_max_rounds", 5)),
        "base_budget": int(entry.get("base_budget", 1)),
        **_base_field_kwargs(entry),
    }
    return CallTypeSubroutine(**kwargs)


def _load_nested_orch(entry: dict[str, Any], source: Path) -> NestedOrchSubroutine:
    key = entry.get("orch_factory_key")
    if not key or not isinstance(key, str):
        raise ValueError(
            f"{source}: nested_orch subroutine {entry.get('name')!r} requires "
            "`orch_factory_key` (string registry key in nested_orchs.py)"
        )
    factory = get_orch_factory(key)
    base_token_cap = entry.get("base_token_cap")
    if base_token_cap is None:
        raise ValueError(
            f"{source}: nested_orch subroutine {entry.get('name')!r} requires "
            "`base_token_cap` (default token sub-cap when not overridden)"
        )
    kwargs: dict[str, Any] = {
        "name": entry["name"],
        "description": entry.get("description", ""),
        "orch_kind": key,
        "factory": factory,
        **_base_field_kwargs(entry),
        # base_token_cap is required for nested_orch (validated above);
        # _base_field_kwargs would only set it if present in YAML, so set
        # the cast int here unconditionally to override its None default.
        "base_token_cap": int(base_token_cap),
    }
    return NestedOrchSubroutine(**kwargs)


def discover_configs(directory: str | Path) -> list[Path]:
    """List all ``*.yaml`` files in ``directory``, sorted by path."""
    directory = Path(directory)
    if not directory.exists():
        return []
    return sorted(p for p in directory.iterdir() if p.suffix in (".yaml", ".yml"))
