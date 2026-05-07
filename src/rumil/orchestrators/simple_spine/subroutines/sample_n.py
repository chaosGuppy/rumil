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

from rumil.llm import LLMExchangeMetadata, text_call
from rumil.model_config import ModelConfig
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
        }
        if self.config_prep is not None:
            out["config_prep"] = self.config_prep.fingerprint()
        return out

    def spawn_tool_schema(self) -> dict[str, Any]:
        properties: dict[str, Any] = {
            "intent": {
                "type": "string",
                "description": (
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
                "description": "Extra context to splice into the user prompt under {additional_context}.",
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

        async def _one(idx: int) -> str:
            cfg = ModelConfig(temperature=self.temperature, max_tokens=self.max_tokens)
            return await text_call(
                self.sys_prompt,
                user_message,
                metadata=LLMExchangeMetadata(
                    call_id=ctx.parent_call_id,
                    phase=f"spawn:{self.name}:sample{idx}",
                ),
                db=ctx.db,
                model=self.model,
                model_config=cfg,
            )

        completions = await asyncio.gather(*[_one(i) for i in range(n)])

        parts: list[str] = [
            f"# {self.name} — {n} samples (intent: {intent[:120]})",
            "",
        ]
        for i, c in enumerate(completions):
            parts.append(f"## Sample {i + 1}")
            parts.append(c.strip())
            parts.append("")
        text_summary = "\n".join(parts)
        return SubroutineResult(text_summary=text_summary, extra={"n": n})
