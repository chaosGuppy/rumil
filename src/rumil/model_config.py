"""Per-call model condition: sampling, thinking, effort.

A frozen bundle of every non-prompt condition that affects the response.
Single source of truth for the shape across rumil internals
(``call_anthropic_api`` / ``structured_call``), forks reproduction,
versus's per-model registry, and any test fixture that previously
constructed loose dicts of the same five fields.

The dataclass itself is pure data — no dependency on
``rumil.llm.thinking_config`` or ``effort_level``. The factory that
derives a default config from a model id lives in :mod:`rumil.llm`
(``derive_model_config``) since that's where the rules live.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class ModelConfig:
    """Complete non-prompt request condition for one LLM call.

    Captures the kwargs that influence the response other than the
    prompt content itself. Frozen so the same instance can be passed
    safely between layers (registry → bridge → rumil → exchange row)
    without mutation surprises.

    ``thinking`` is the Anthropic adaptive/extended-thinking dict (e.g.
    ``{"type": "adaptive", "display": "summarized"}``) or ``None``.
    ``effort`` is wrapped on the wire as ``output_config.effort``.
    Direct anthropic_client paths today set both to ``None``; bridge
    paths populate them from rumil's model rules (or, with the versus
    registry, from the operator's declarative config).
    """

    temperature: float | None
    max_tokens: int
    top_p: float | None = None
    thinking: dict[str, Any] | None = None
    effort: str | None = None

    def to_anthropic_kwargs(self) -> dict[str, Any]:
        """Build the messages.create kwargs subset for this config.

        Only emits keys that should actually go on the wire — None values
        are dropped (the API rejects null thinking, etc.). Callers merge
        this into the full kwargs dict (with model, system, messages,
        tools).
        """
        kwargs: dict[str, Any] = {"max_tokens": self.max_tokens}
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        if self.top_p is not None:
            kwargs["top_p"] = self.top_p
        if self.thinking is not None:
            kwargs["thinking"] = dict(self.thinking)
        if self.effort is not None:
            kwargs["output_config"] = {"effort": self.effort}
        return kwargs

    def to_record_dict(self) -> dict[str, Any]:
        """Canonical dict for storage and hashing.

        Always includes every field — None values are kept so the
        canonical hash forks deterministically when a previously-None
        condition becomes set (or vice versa). Mirror of
        ``to_anthropic_kwargs`` but null-preserving.
        """
        return asdict(self)


def model_config_from_record(record: dict[str, Any]) -> ModelConfig:
    """Inverse of ``ModelConfig.to_record_dict`` for stored rows.

    Tolerant of missing fields so legacy rows (pre-this-schema) parse
    sensibly. Callers handle the "no record at all" case (returning
    ``None``); this just deserializes a dict that's already known to
    represent a config.
    """
    return ModelConfig(
        temperature=record.get("temperature"),
        max_tokens=record["max_tokens"],
        top_p=record.get("top_p"),
        thinking=record.get("thinking"),
        effort=record.get("effort"),
    )
