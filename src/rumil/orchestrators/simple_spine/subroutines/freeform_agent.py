"""FreeformAgentSubroutine — generic tool-using agent loop.

The mainline agent's most flexible spawn primitive. Configurable
sys/user prompt, model, max rounds, and a list of tool names looked up
from the SimpleSpine tool registry. Optionally allows recursion (the
spawned agent can itself fire spawn tools — see ``allow_recurse``).

When ``config_prep`` is set, the spawn tool exposes a thin ``intent``
schema and a hidden config-prep call elaborates the full agent config.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from rumil.model_config import ModelConfig
from rumil.orchestrators.simple_spine.agent_loop import thin_agent_loop
from rumil.orchestrators.simple_spine.subroutines.base import (
    ConfigPrepDef,
    SpawnCtx,
    SubroutineResult,
)


def _sha8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def _load_prompt(path: str | Path | None, default: str) -> str:
    if path is None:
        return default
    text = Path(path).read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError(f"prompt file is empty or whitespace-only: {path}")
    return text


class FreeformAgentPreppedConfig(BaseModel):
    """Schema returned by a config-prep call for FreeformAgentSubroutine.

    Optional fields fall back to the SubroutineDef's static config. When
    no ``config_prep`` is set on the def, this type is unused.
    """

    sys_prompt: str | None = None
    user_prompt: str | None = None
    additional_context: str | None = None
    enabled_tools: list[str] | None = None
    max_rounds: int | None = None


@dataclass(frozen=True)
class FreeformAgentSubroutine:
    """Configurable agent loop with arbitrary tools.

    ``tool_factory`` is a callable that, given the SpawnCtx and the list
    of enabled tool names, returns a list of :class:`rumil.llm.Tool`
    instances. Decoupling tool wiring from the SubroutineDef keeps the
    library plain-data and lets a single tool registry serve every
    subroutine that opts into tools.
    """

    name: str
    description: str
    sys_prompt: str
    user_prompt_template: str
    model: str
    max_rounds: int = 5
    max_tokens: int = 4096
    allowed_tool_names: tuple[str, ...] = ()
    sys_prompt_path: str | Path | None = None
    overridable: frozenset[str] = field(
        default_factory=lambda: frozenset({"intent", "additional_context"})
    )
    config_prep: ConfigPrepDef | None = None

    def __post_init__(self) -> None:
        if self.max_rounds < 1:
            raise ValueError(f"max_rounds must be >= 1, got {self.max_rounds}")
        if self.sys_prompt_path is not None:
            object.__setattr__(
                self,
                "sys_prompt",
                _load_prompt(self.sys_prompt_path, self.sys_prompt),
            )

    def fingerprint(self) -> Mapping[str, Any]:
        out: dict[str, Any] = {
            "kind": "freeform_agent",
            "name": self.name,
            "model": self.model,
            "sys_prompt_hash": _sha8(self.sys_prompt),
            "user_prompt_template_hash": _sha8(self.user_prompt_template),
            "max_rounds": self.max_rounds,
            "max_tokens": self.max_tokens,
            "allowed_tool_names": sorted(self.allowed_tool_names),
            "overridable": sorted(self.overridable),
        }
        if self.config_prep is not None:
            out["config_prep"] = self.config_prep.fingerprint()
        return out

    def spawn_tool_schema(self) -> dict[str, Any]:
        properties: dict[str, Any] = {
            "intent": {
                "type": "string",
                "description": (
                    "Short statement of what you want this agent to do. "
                    "Substituted into the user prompt template as {intent}."
                ),
            },
        }
        required = ["intent"]
        if "additional_context" in self.overridable:
            properties["additional_context"] = {
                "type": "string",
                "description": (
                    "Extra context / scratchpad excerpts to splice into the "
                    "user prompt under {additional_context}."
                ),
            }
        if "max_rounds" in self.overridable:
            properties["max_rounds"] = {
                "type": "integer",
                "minimum": 1,
                "maximum": self.max_rounds,
                "description": (f"Cap rounds for this spawn (default {self.max_rounds})."),
            }
        return {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }

    async def run(self, ctx: SpawnCtx, overrides: Mapping[str, Any]) -> SubroutineResult:
        prepped = (
            ctx.prepped_config
            if isinstance(ctx.prepped_config, FreeformAgentPreppedConfig)
            else None
        )

        sys_prompt = prepped.sys_prompt if prepped and prepped.sys_prompt else self.sys_prompt
        max_rounds_override = overrides.get("max_rounds")
        max_rounds = (
            int(max_rounds_override)
            if max_rounds_override is not None and "max_rounds" in self.overridable
            else (prepped.max_rounds if prepped and prepped.max_rounds else self.max_rounds)
        )

        if prepped and prepped.user_prompt:
            user_message = prepped.user_prompt
        else:
            intent = str(overrides.get("intent", ""))
            additional_context = (
                prepped.additional_context
                if prepped and prepped.additional_context is not None
                else str(overrides.get("additional_context", ""))
            )
            try:
                user_message = self.user_prompt_template.format(
                    intent=intent,
                    additional_context=additional_context,
                    operating_assumptions="",
                )
            except KeyError as e:
                raise ValueError(
                    f"freeform_agent {self.name!r}: user_prompt_template references unknown key {e}"
                ) from e

        enabled_tool_names: Sequence[str] = (
            prepped.enabled_tools
            if prepped and prepped.enabled_tools is not None
            else self.allowed_tool_names
        )
        # Tool resolution lives in the orchestrator's tool registry, which
        # is reachable via ctx.spawn_id-keyed lookup. To avoid a circular
        # import the registry is imported lazily here.
        from rumil.orchestrators.simple_spine.tools import resolve_tools

        tools = resolve_tools(enabled_tool_names, ctx)
        cfg = ModelConfig(temperature=1.0, max_tokens=self.max_tokens)
        messages: list[dict] = [{"role": "user", "content": user_message}]
        result = await thin_agent_loop(
            system_prompt=sys_prompt,
            messages=messages,
            tools=tools,
            model=self.model,
            model_config=cfg,
            db=ctx.db,
            call_id=ctx.parent_call_id,
            phase=f"spawn:{self.name}",
            budget_clock=ctx.budget_clock,
            max_rounds=max_rounds,
            cache=True,
        )
        text_summary = (
            f"# {self.name}\n"
            f"_(rounds={result.rounds}, stopped_because={result.stopped_because})_\n\n"
            f"{result.final_text}"
        )
        return SubroutineResult(
            text_summary=text_summary,
            extra={
                "rounds": result.rounds,
                "stopped_because": result.stopped_because,
                "tool_call_count": len(result.tool_calls),
            },
        )
