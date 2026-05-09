"""FreeformAgentSubroutine — generic tool-using agent loop.

The mainline agent's most flexible spawn primitive. Configurable
sys/user prompt, model, max rounds, and a list of tool names looked up
from the SimpleSpine tool registry. Optionally allows recursion (the
spawned agent can itself fire spawn tools — see ``allow_recurse``).

When ``config_prep`` is set, the spawn tool exposes a thin ``intent``
schema and a hidden config-prep call branches off mainline (same
system + history) to elaborate the full agent config.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from rumil.model_config import ModelConfig
from rumil.orchestrators.simple_spine.agent_loop import thin_agent_loop
from rumil.orchestrators.simple_spine.subroutines.base import (
    SpawnCtx,
    SubroutineBase,
    SubroutineResult,
    load_prompt,
    sha8,
)

log = logging.getLogger(__name__)


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


@dataclass(frozen=True, kw_only=True)
class FreeformAgentSubroutine(SubroutineBase):
    """Configurable agent loop with arbitrary tools.

    Inherits the cross-cutting fields from :class:`SubroutineBase`.
    Honors ``inherit_assumptions`` (spliced into sys_prompt at run time)
    and ``base_cost_cap_usd`` (carves a child BudgetClock).
    """

    sys_prompt: str
    user_prompt_template: str
    model: str
    max_rounds: int = 5
    max_tokens: int = 4096
    allowed_tool_names: tuple[str, ...] = ()
    # Anthropic prompt caching toggle. Default True (multi-round agents
    # amortise the cache write across rounds). Single-shot drafters
    # (``max_rounds=1``) should set ``cache: false`` in YAML — cache
    # writes cost ~25% more than regular input and never get read back,
    # so caching on a single call is a net loss. Multi-spawn parallel
    # agents that share a prefix benefit even without multiple rounds
    # since later spawns hit the cache the first one wrote.
    cache: bool = True
    sys_prompt_path: str | Path | None = None
    overridable: frozenset[str] = field(
        default_factory=lambda: frozenset({"intent", "additional_context"})
    )
    # Optional post-hoc validation on the final response text. When the
    # validator returns False, the subroutine appends ``retry_message``
    # to the conversation and re-runs the inner agent loop, up to
    # ``response_max_retries`` extra attempts. Used for wire-format
    # constraints like the 7-point preference label on the verdict
    # subroutine — without retry, a single off-script response from the
    # model becomes a NULL judgment row.
    response_validator: Callable[[str], bool] | None = None
    retry_message: str = ""
    response_max_retries: int = 1
    # Stable name folded into the fingerprint in lieu of the validator
    # callable itself (callables don't hash). Required when
    # ``response_validator`` is set so that two subroutines using
    # different validators don't fingerprint identically.
    response_validator_name: str | None = None

    def __post_init__(self) -> None:
        if self.max_rounds < 1:
            raise ValueError(f"max_rounds must be >= 1, got {self.max_rounds}")
        if self.sys_prompt_path is not None:
            object.__setattr__(
                self,
                "sys_prompt",
                load_prompt(self.sys_prompt_path, self.sys_prompt),
            )
        if self.response_validator is not None and self.response_validator_name is None:
            raise ValueError(
                f"freeform_agent {self.name!r}: response_validator is set but "
                "response_validator_name is None — name is required so the "
                "fingerprint distinguishes which validator was applied"
            )
        if self.response_validator is not None and not self.retry_message.strip():
            raise ValueError(
                f"freeform_agent {self.name!r}: response_validator is set but "
                "retry_message is empty — the retry needs a tightening message "
                "or the model gets the same prompt and likely the same output"
            )

    def fingerprint(self) -> Mapping[str, Any]:
        out = dict(super().fingerprint())
        out["kind"] = "freeform_agent"
        out["model"] = self.model
        out["sys_prompt_hash"] = sha8(self.sys_prompt)
        out["user_prompt_template_hash"] = sha8(self.user_prompt_template)
        out["max_rounds"] = self.max_rounds
        out["max_tokens"] = self.max_tokens
        out["allowed_tool_names"] = sorted(self.allowed_tool_names)
        out["cache"] = self.cache
        if self.response_validator is not None:
            out["response_validator_name"] = self.response_validator_name
            out["retry_message_hash"] = sha8(self.retry_message)
            out["response_max_retries"] = self.response_max_retries
        return out

    def _default_intent_description(self) -> str:
        return (
            "Short statement of what you want this agent to do. "
            "Substituted into the user prompt template as {intent}."
        )

    def _extra_schema_properties(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if "max_rounds" in self.overridable:
            out["max_rounds"] = {
                "type": "integer",
                "minimum": 1,
                "maximum": self.max_rounds,
                "description": (f"Cap rounds for this spawn (default {self.max_rounds})."),
            }
        return out

    async def run(self, ctx: SpawnCtx, overrides: Mapping[str, Any]) -> SubroutineResult:
        prepped = (
            ctx.prepped_config
            if isinstance(ctx.prepped_config, FreeformAgentPreppedConfig)
            else None
        )

        sys_prompt = prepped.sys_prompt if prepped and prepped.sys_prompt else self.sys_prompt
        sys_prompt = self.apply_assumptions(sys_prompt, ctx)
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

        artifact_block = self.render_artifact_block(ctx)
        if artifact_block:
            user_message = artifact_block + "\n" + user_message

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
        # ctx.budget_clock is already the per-spawn child carved by the
        # orchestrator (see SubroutineBase.carve_spawn_clock + the
        # _run_spawn dispatch site). Use it directly.
        spawn_clock = ctx.budget_clock
        result = await thin_agent_loop(
            system_prompt=sys_prompt,
            messages=messages,
            tools=tools,
            model=self.model,
            model_config=cfg,
            db=ctx.db,
            call_id=ctx.parent_call_id,
            phase=f"spawn:{self.name}",
            budget_clock=spawn_clock,
            max_rounds=max_rounds,
            cache=self.cache,
        )

        retries_used = 0
        if self.response_validator is not None:
            for attempt in range(1, self.response_max_retries + 1):
                if self.response_validator(result.final_text):
                    break
                if spawn_clock.cost_exhausted:
                    log.warning(
                        "freeform_agent %s: validator failed but token budget "
                        "exhausted; returning last response unvalidated",
                        self.name,
                    )
                    break
                log.info(
                    "freeform_agent %s: response_validator failed, retry %d/%d",
                    self.name,
                    attempt,
                    self.response_max_retries,
                )
                messages.append({"role": "user", "content": self.retry_message})
                result = await thin_agent_loop(
                    system_prompt=sys_prompt,
                    messages=messages,
                    tools=tools,
                    model=self.model,
                    model_config=cfg,
                    db=ctx.db,
                    call_id=ctx.parent_call_id,
                    phase=f"spawn:{self.name}:retry{attempt}",
                    budget_clock=spawn_clock,
                    max_rounds=max_rounds,
                    cache=self.cache,
                )
                retries_used = attempt

        text_summary = (
            f"# {self.name}\n"
            f"_(rounds={result.rounds}, stopped_because={result.stopped_because}"
            f"{f', validator_retries={retries_used}' if retries_used else ''})_\n\n"
            f"{result.final_text}"
        )
        # Default artifact: the agent's final_text (body only, no header).
        # Orchestrator namespaces this under <name>/<spawn_id_short>.
        # Subroutines wanting multi-key outputs can override produces with
        # {"key1": ..., "key2": ...}; an empty produces means "no
        # artifact contributed" (rare for FreeformAgent).
        return SubroutineResult(
            text_summary=text_summary,
            extra={
                "rounds": result.rounds,
                "stopped_because": result.stopped_because,
                "tool_call_count": len(result.tool_calls),
                "validator_retries": retries_used,
            },
            produces={"": result.final_text},
        )
