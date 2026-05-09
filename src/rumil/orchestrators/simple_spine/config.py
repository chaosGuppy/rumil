"""SimpleSpineConfig + OrchInputs — the data the orch is parameterised by.

The config is a frozen dataclass with a content-addressed fingerprint;
two configs that produce identical fingerprints behave identically.
Versus uses the fingerprint as a dedup key alongside the existing
versus_texts / versus_judgments hashes.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
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
    # When True, the orchestrator wires `read_artifact` and
    # `search_artifacts` tools onto the mainline agent so it can pull
    # a fetched source's full text into context or scan across the
    # run's accumulated artifacts. Off by default — versus configs are
    # blind/scoped and don't want extra surfaces; research-style configs
    # flip this on so mainline can browse what its spawns produced
    # (e.g. web_research's per-source artifacts).
    expose_artifact_tools: bool = False
    # Preset-level defaults for the OrchInputs fields that shape the
    # finalize deliverable. When a caller leaves the corresponding
    # OrchInputs field at its empty/None default, the orchestrator
    # falls back to these. Lets a preset (e.g. view_freeform) be
    # self-describing — pick the preset name and you get view-shaped
    # output without separately passing schema + guidance.
    # Schema is stored as a JSON Schema dict (not a Pydantic class) so
    # it can ride along inside the YAML-driven config; the orchestrator
    # already supports both shapes for OrchInputs.output_schema, with
    # dict triggering the lighter coercion path.
    default_output_guidance: str | None = None
    default_output_schema: Mapping[str, Any] | None = None

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
            "expose_artifact_tools": self.expose_artifact_tools,
            "default_output_guidance_hash": (
                sha8(self.default_output_guidance) if self.default_output_guidance else None
            ),
            "default_output_schema_hash": (
                sha8(json.dumps(dict(self.default_output_schema), sort_keys=True))
                if self.default_output_schema is not None
                else None
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
            "expose_artifact_tools": self.expose_artifact_tools,
            "default_output_guidance_hash": (
                sha8(self.default_output_guidance) if self.default_output_guidance else None
            ),
            "default_output_schema_hash": (
                sha8(json.dumps(dict(self.default_output_schema), sort_keys=True))
                if self.default_output_schema is not None
                else None
            ),
            "subroutines": [s.fingerprint() for s in self.process_library],
            "fingerprint": self.fingerprint,
        }


# Per https://platform.claude.com/docs/en/build-with-claude/compaction,
# Anthropic's ``compact_20260112`` server-side strategy is supported on
# Opus 4.7, Opus 4.6, and Sonnet 4.6. Haiku 4.5 is not — the API returns
# 400 ``does not support the 'compact_20260112' context management
# strategy``. Conservative list (known-unsupported, default to assuming
# support) so a new compaction-capable model lands working out of the
# box; if a future model is also unsupported, append its prefix here.
_MODELS_WITHOUT_COMPACTION_PREFIXES: tuple[str, ...] = ("claude-haiku-",)


def model_supports_compaction(model: str) -> bool:
    """True if ``model`` supports Anthropic's ``compact_20260112`` strategy."""
    return not model.startswith(_MODELS_WITHOUT_COMPACTION_PREFIXES)


def apply_model_override(cfg: SimpleSpineConfig, model: str) -> SimpleSpineConfig:
    """Return a copy of ``cfg`` with every model reference set to ``model``.

    Replaces ``main_model`` plus every ``.model`` field on subroutines in
    ``process_library``. Subroutines without a ``.model`` field (currently
    ``CallTypeSubroutine`` and ``NestedOrchSubroutine`` — they delegate to
    other configs / preset names) pass through unchanged. Nested-orch
    children pick up the same override at run time via the
    ``simple_spine_model_override`` setting (read by ``_simple_spine_recurse``).

    Also force-disables ``enable_server_compaction`` when the override
    targets a model that doesn't support ``compact_20260112`` (currently
    Haiku) — sending the beta header to such models 400s. Models that do
    support compaction (Opus 4.6/4.7, Sonnet 4.6) keep the YAML setting.

    Smoke-test convenience: lets ``--model claude-haiku-4-5-...`` produce
    a single-model run end-to-end without touching the YAML.
    """
    from dataclasses import replace as _dc_replace

    new_subs: list[SubroutineDef] = []
    for sub in cfg.process_library:
        # Only kinds that own an LLM call have a .model attr; nested_orch
        # / call_type point at preset names / external runners.
        if hasattr(sub, "model"):
            new_subs.append(_dc_replace(sub, model=model))  # pyright: ignore[reportArgumentType, reportCallIssue]
        else:
            new_subs.append(sub)
    new_cfg = _dc_replace(cfg, main_model=model, process_library=tuple(new_subs))
    if cfg.enable_server_compaction and not model_supports_compaction(model):
        new_cfg = _dc_replace(new_cfg, enable_server_compaction=False)
    return new_cfg


@dataclass
class OrchInputs:
    """Per-invocation inputs for one SimpleSpine run.

    These are NOT folded into the config fingerprint — different inputs
    against the same config are different runs of the same variant.

    ``artifacts`` is a caller-seeded k,v map of named text blobs (pair
    surface, rubric, scoped question text, etc.). Subroutines reference
    entries by key via static :class:`SubroutineBase.consumes` or via
    mainline-supplied ``include_artifacts`` on the spawn tool — see
    :mod:`rumil.orchestrators.simple_spine.artifacts`. The store also
    accumulates outputs from spawns under
    ``<sub_name>/<spawn_id>[/<sub_key>]`` keys at run time.
    """

    question_id: str
    additional_context: str = ""
    operating_assumptions: str = ""
    output_guidance: str = ""
    # ``output_schema`` is rendered into the mainline's first user turn so
    # the model knows what shape its finalize answer should take. A
    # Pydantic class triggers a post-hoc structured-call coercion (full
    # schema enforcement); a raw JSON Schema dict triggers a lighter
    # text-call coercion that parses JSON but does not validate against
    # the schema (callers parse / validate themselves on the dict path).
    # Dict shape is what tool-call callers pass — Pydantic classes can't
    # cross a JSON tool boundary.
    output_schema: type[BaseModel] | dict[str, Any] | None = None
    budget: BudgetSpec = field(default_factory=lambda: BudgetSpec(max_tokens=200_000))
    artifacts: Mapping[str, str] = field(default_factory=dict)


@dataclass
class OrchResult:
    """Return value of SimpleSpineOrchestrator.run."""

    answer_text: str
    structured_answer: BaseModel | dict[str, Any] | None
    fingerprint: str
    call_id: str
    spawn_count: int
    tokens_used: int
    elapsed_s: float
    finalize_reason: str
    last_status: str = "complete"
