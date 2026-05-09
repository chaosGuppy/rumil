"""SimpleSpineConfig + OrchInputs — the data the orch is parameterised by.

The config is a frozen dataclass with a content-addressed fingerprint;
two configs that produce identical fingerprints behave identically.
Versus uses the fingerprint as a dedup key alongside the existing
versus_texts / versus_judgments hashes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from functools import cached_property
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from rumil.orchestrators.simple_spine.budget_clock import BudgetSpec
from rumil.orchestrators.simple_spine.subroutines.base import sha8

if TYPE_CHECKING:
    from rumil.orchestrators.simple_spine.subroutines.base import SubroutineDef


@dataclass(frozen=True)
class SimpleSpineConfig:
    """Configuration for one SimpleSpine variant.

    Edit any field — including a subroutine's prompt or model — and the
    fingerprint changes deterministically. The fingerprint is the natural
    A/B key for versus / iterate workflows.
    """

    main_model: str
    process_library: tuple[SubroutineDef, ...]
    main_system_prompt: str
    enable_finalize_tool: bool = True
    # Soft cap on parallel spawn tool calls per turn. None = unlimited.
    # If mainline emits more than this many spawn tool calls in a single
    # turn, the excess get "throttled" tool results telling the agent to
    # try again next round; results from the kept ones still come back.
    max_parallel_spawns_per_turn: int | None = None
    # Per-turn ModelConfig for the mainline agent. ``mainline_max_tokens``
    # caps each round's assistant output (including the finalize tool's
    # ``answer`` payload — set this large enough to fit the full
    # deliverable when the model finalizes in a single turn).
    # Default 8192 is sized for typical orchestration / research use;
    # the versus presets (essay_continuation, judge_pair) pin 32_000
    # explicitly because their finalize.answer can carry a full essay
    # continuation or a verdict that quotes long sub-results inline.
    mainline_temperature: float = 1.0
    mainline_max_tokens: int = 8_192
    # When tokens are exhausted, the next mainline turn is invoked with a
    # forced-finalize system reminder. If the agent still doesn't call
    # finalize on that turn, the orch synthesizes a finalize from the
    # last assistant text. Set False to let the run end without a finalize
    # (return value will carry an empty answer).
    force_finalize_on_token_exhaustion: bool = True
    # Anthropic server-side compaction (compact_20260112). When enabled,
    # the API auto-summarizes the mainline thread once input tokens cross
    # ``compaction_trigger_tokens``; subsequent turns continue from the
    # summary with the prefix dropped. ``compaction_instructions`` fully
    # replaces the default summarization prompt when set — see
    # https://platform.claude.com/docs/en/build-with-claude/compaction.
    enable_server_compaction: bool = False
    compaction_trigger_tokens: int = 150_000
    compaction_instructions: str | None = None

    @cached_property
    def fingerprint(self) -> str:
        """sha256 of a canonical-form dump. First 12 hex chars usable as a tag."""
        blob = {
            "main_model": self.main_model,
            "main_system_prompt_hash": sha8(self.main_system_prompt),
            "enable_finalize_tool": self.enable_finalize_tool,
            "max_parallel_spawns_per_turn": self.max_parallel_spawns_per_turn,
            "mainline_temperature": self.mainline_temperature,
            "mainline_max_tokens": self.mainline_max_tokens,
            "force_finalize_on_token_exhaustion": self.force_finalize_on_token_exhaustion,
            "enable_server_compaction": self.enable_server_compaction,
            "compaction_trigger_tokens": self.compaction_trigger_tokens,
            "compaction_instructions_hash": (
                sha8(self.compaction_instructions) if self.compaction_instructions else None
            ),
            "subroutines": [s.fingerprint() for s in self.process_library],
        }
        canonical = json.dumps(blob, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @property
    def fingerprint_short(self) -> str:
        return self.fingerprint[:12]

    def fingerprint_dict(self) -> dict[str, Any]:
        """Decomposed fingerprint for trace inspection / call_params."""
        return {
            "main_model": self.main_model,
            "main_system_prompt_hash": sha8(self.main_system_prompt),
            "enable_finalize_tool": self.enable_finalize_tool,
            "max_parallel_spawns_per_turn": self.max_parallel_spawns_per_turn,
            "mainline_temperature": self.mainline_temperature,
            "mainline_max_tokens": self.mainline_max_tokens,
            "force_finalize_on_token_exhaustion": self.force_finalize_on_token_exhaustion,
            "enable_server_compaction": self.enable_server_compaction,
            "compaction_trigger_tokens": self.compaction_trigger_tokens,
            "compaction_instructions_hash": (
                sha8(self.compaction_instructions) if self.compaction_instructions else None
            ),
            "subroutines": [s.fingerprint() for s in self.process_library],
            "fingerprint": self.fingerprint,
        }


@dataclass
class OrchInputs:
    """Per-invocation inputs for one SimpleSpine run.

    These are NOT folded into the config fingerprint — different inputs
    against the same config are different runs of the same variant.
    """

    question_id: str
    additional_context: str = ""
    operating_assumptions: str = ""
    output_guidance: str = ""
    output_schema: type[BaseModel] | None = None
    budget: BudgetSpec = field(default_factory=lambda: BudgetSpec(max_tokens=200_000))


@dataclass
class OrchResult:
    """Return value of SimpleSpineOrchestrator.run."""

    answer_text: str
    structured_answer: BaseModel | None
    fingerprint: str
    call_id: str
    spawn_count: int
    tokens_used: int
    elapsed_s: float
    finalize_reason: str
    last_status: str = "complete"
