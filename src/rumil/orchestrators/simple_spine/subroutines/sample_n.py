"""SampleNSubroutine — fire one prompt N times in parallel.

No tools, no agent loop. Designed for ensembling / diverse-completion
patterns where the mainline agent wants several independent samples and
plans to pick or aggregate in a later turn.

The text summary returned to mainline interleaves all N completions with
clear delimiters; mainline reads them as separate items in its next turn.
For long completions this can blow up tokens fast — keep ``n`` modest
(2-5) and consider using ``ConfigPrepDef`` to let the prep call decide N.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anthropic
from anthropic.types import TextBlock

from rumil.llm import LLMExchangeMetadata, call_anthropic_api
from rumil.model_config import ModelConfig
from rumil.orchestrators.simple_spine.subroutines.base import (
    ConfigPrepDef,
    SpawnCtx,
    SubroutineResult,
    resolve_spawn_clock,
)
from rumil.settings import get_settings


def _sha8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def _estimate_per_sample_worst(sys_prompt: str, user_message: str, max_tokens: int) -> int:
    """Conservative upper bound on tokens one sample will consume.

    Input is estimated via a ~4-chars-per-token heuristic (rough but
    sufficient — the dominant term is usually max_tokens, and we already
    use the *worst-case* output here). Output is bounded by ``max_tokens``.

    The estimate is used to decide how many samples can be safely
    launched in parallel without overshooting a token cap. Underestimating
    input tokens by 2× still leaves a substantial safety margin because
    real outputs are typically well below max_tokens.
    """
    input_estimate = (len(sys_prompt) + len(user_message)) // 4
    return input_estimate + max_tokens


def _load_prompt(path: str | Path | None, default: str) -> str:
    if path is None:
        return default
    text = Path(path).read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError(f"prompt file is empty or whitespace-only: {path}")
    return text


@dataclass(frozen=True)
class SampleNSubroutine:
    """Fire ``user_prompt_template`` to ``model`` ``n`` times in parallel.

    The template is rendered with the override dict via ``.format(**overrides)``
    plus the keys ``additional_context`` and ``operating_assumptions``
    that the orchestrator always passes through.
    """

    name: str
    description: str
    sys_prompt: str
    user_prompt_template: str
    model: str
    n: int = 3
    temperature: float = 1.0
    max_tokens: int = 4096
    sys_prompt_path: str | Path | None = None
    overridable: frozenset[str] = field(default_factory=lambda: frozenset({"intent", "n"}))
    config_prep: ConfigPrepDef | None = None
    # See FreeformAgentSubroutine for semantics. Enforcement here is
    # affordability-aware: the run loop estimates a worst-case per-sample
    # token cost (rough input estimate + max_tokens) and only launches as
    # many samples per batch as fit in the carved clock's remaining
    # budget. After each batch, the clock reflects actual (typically
    # below worst-case) spend, so subsequent batches may launch more.
    # When budget is comfortable this collapses to a single batch of N
    # (full parallelism); when tight it degrades to smaller batches and
    # may skip some samples entirely (text_summary indicates how many
    # ran vs were skipped). The parent clock still receives every
    # sample's spend via carve_child rollup. Slight overshoot is
    # possible when actual usage exceeds the worst-case estimate (rare,
    # since max_tokens is the dominant term and is a hard ceiling).
    base_token_cap: int | None = None
    cost_hint: str | None = None
    # See FreeformAgentSubroutine for semantics.
    intent_description: str | None = None
    additional_context_description: str | None = None
    # When True, caller-supplied operating_assumptions (threaded via
    # SpawnCtx) are appended to this subroutine's system prompt at run
    # time. Default True; opt out when bias would distort the role
    # (e.g. independent critics whose job is to challenge framings).
    inherit_assumptions: bool = True

    def __post_init__(self) -> None:
        if self.n < 1:
            raise ValueError(f"n must be >= 1, got {self.n}")
        # Resolve prompt content at construction so fingerprint() is stable.
        if self.sys_prompt_path is not None:
            object.__setattr__(
                self, "sys_prompt", _load_prompt(self.sys_prompt_path, self.sys_prompt)
            )

    def fingerprint(self) -> Mapping[str, Any]:
        out: dict[str, Any] = {
            "kind": "sample_n",
            "name": self.name,
            "model": self.model,
            "sys_prompt_hash": _sha8(self.sys_prompt),
            "user_prompt_template_hash": _sha8(self.user_prompt_template),
            "n": self.n,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "overridable": sorted(self.overridable),
            "inherit_assumptions": self.inherit_assumptions,
            "base_token_cap": self.base_token_cap,
        }
        if self.config_prep is not None:
            out["config_prep"] = self.config_prep.fingerprint()
        return out

    def spawn_tool_schema(self) -> dict[str, Any]:
        properties: dict[str, Any] = {
            "intent": {
                "type": "string",
                "description": self.intent_description
                or (
                    "Short statement of what you want this batch of samples to "
                    "address. Substituted into the user prompt template as "
                    "{intent}."
                ),
            },
        }
        required = ["intent"]
        if "n" in self.overridable:
            properties["n"] = {
                "type": "integer",
                "minimum": 1,
                "maximum": 10,
                "description": (
                    f"Number of parallel samples (default {self.n}). Higher "
                    "diversity but proportional token spend."
                ),
            }
        if "additional_context" in self.overridable:
            properties["additional_context"] = {
                "type": "string",
                "description": self.additional_context_description
                or ("Extra context to splice into the user prompt under {additional_context}."),
            }
        if "token_cap" in self.overridable and self.base_token_cap is not None:
            properties["token_cap"] = {
                "type": "integer",
                "minimum": 500,
                "description": (
                    f"Per-spawn token budget covering all N samples "
                    f"(default {self.base_token_cap}). Samples are launched "
                    "in batches sized to fit the remaining cap; if the cap "
                    "is too tight for all N, some samples are skipped (the "
                    "text_summary reports run/total). Tokens still debit "
                    "the parent budget. Capped at the parent's remaining."
                ),
            }
        return {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }

    async def run(self, ctx: SpawnCtx, overrides: Mapping[str, Any]) -> SubroutineResult:
        n = int(overrides.get("n", self.n)) if "n" in self.overridable else self.n
        intent = str(overrides.get("intent", ""))
        additional_context = str(overrides.get("additional_context", ""))

        format_kwargs = {
            "intent": intent,
            "additional_context": additional_context,
            "operating_assumptions": "",
        }
        try:
            user_message = self.user_prompt_template.format(**format_kwargs)
        except KeyError as e:
            raise ValueError(
                f"sample_n subroutine {self.name!r}: user_prompt_template "
                f"references unknown key {e}; supported keys: "
                f"{sorted(format_kwargs)}"
            ) from e

        sys_prompt = self.sys_prompt
        if self.inherit_assumptions and ctx.operating_assumptions.strip():
            sys_prompt = sys_prompt.rstrip() + (
                "\n\n## Operating assumptions\n\n" + ctx.operating_assumptions.strip() + "\n"
            )

        spawn_clock = resolve_spawn_clock(
            ctx.budget_clock,
            base_cap=self.base_token_cap,
            override_cap=overrides.get("token_cap") if "token_cap" in self.overridable else None,
        )
        client = anthropic.AsyncAnthropic(api_key=get_settings().require_anthropic_key())
        cfg = ModelConfig(temperature=self.temperature, max_tokens=self.max_tokens)
        msgs: list[dict] = [{"role": "user", "content": user_message}]

        async def _one(idx: int) -> str:
            api_resp = await call_anthropic_api(
                client,
                self.model,
                sys_prompt,
                msgs,
                metadata=LLMExchangeMetadata(
                    call_id=ctx.parent_call_id,
                    phase=f"spawn:{self.name}:sample{idx}",
                ),
                db=ctx.db,
                cache=False,
                model_config=cfg,
            )
            usage = api_resp.message.usage
            if usage is not None:
                spawn_clock.record_tokens((usage.input_tokens or 0) + (usage.output_tokens or 0))
            for block in api_resp.message.content:
                if isinstance(block, TextBlock):
                    return block.text
            return ""

        # Affordability-aware launching. Pick a worst-case per-sample size
        # (rough input estimate + max_tokens), then in each iteration
        # launch as many samples as fit in the remaining clock and await
        # them. After they finish, the clock has updated to reflect actual
        # (typically lower-than-worst-case) spend, so we may have room
        # for more. Stops when all N have run or the clock can't fit one
        # more worst-case sample. Yields full parallelism when budget is
        # comfortable (one big batch); falls back to smaller batches as
        # the cap tightens, instead of overshooting it.
        per_sample_worst = _estimate_per_sample_worst(sys_prompt, user_message, self.max_tokens)
        completions: list[str] = []
        launched = 0
        while launched < n:
            affordable = (
                spawn_clock.tokens_remaining // per_sample_worst if per_sample_worst > 0 else n
            )
            if affordable <= 0:
                break
            batch_size = min(int(affordable), n - launched)
            batch = await asyncio.gather(*[_one(launched + i) for i in range(batch_size)])
            completions.extend(batch)
            launched += batch_size
        skipped = n - launched

        header = f"# {self.name} — {len(completions)}/{n} samples (intent: {intent[:120]})"
        if skipped:
            header += f" — {skipped} skipped (token cap)"
        parts: list[str] = [header, ""]
        for i, c in enumerate(completions):
            parts.append(f"## Sample {i + 1}")
            parts.append(c.strip())
            parts.append("")
        text_summary = "\n".join(parts)
        return SubroutineResult(
            text_summary=text_summary,
            extra={"n": n, "samples_run": len(completions), "samples_skipped": skipped},
        )
