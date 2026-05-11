"""Response-validator registry for FreeformAgentSubroutine.

The YAML loader resolves a string name (``response_validator: <name>``)
to a ``Callable[[str], bool]`` via this registry. Validators must be
deterministic, side-effect-free predicates over the agent's final
response text — they return True when the response is acceptable.

Built-in validators are registered at module import time. Add new ones
via :func:`register_validator`; new YAML configs that reference an
unknown name will fail loudly at config-load time.
"""

from __future__ import annotations

from collections.abc import Callable

ResponseValidator = Callable[[str], bool]

_REGISTRY: dict[str, ResponseValidator] = {}


def register_validator(name: str, fn: ResponseValidator) -> None:
    """Register a validator under ``name``.

    Idempotent: re-registering the same name silently overwrites — keeps
    test fixtures and module-level register calls safe to re-run.
    """
    _REGISTRY[name] = fn


def get_validator(name: str) -> ResponseValidator:
    if name not in _REGISTRY:
        known = sorted(_REGISTRY)
        raise KeyError(f"unknown response_validator {name!r}; registered: {known}")
    return _REGISTRY[name]


def list_validators() -> list[str]:
    return sorted(_REGISTRY)


def _extract_preference_not_none(text: str) -> bool:
    """True iff the text ends with one of the seven canonical labels.

    Used by the judge_pair verdict subroutine — without retry on a None
    parse, a single off-script response from the model becomes a NULL
    judgment row.
    """
    from rumil.versus_prompts import extract_preference

    return extract_preference(text) is not None


register_validator("extract_preference_not_none", _extract_preference_not_none)
