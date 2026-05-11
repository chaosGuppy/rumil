"""Run-level config + inputs + result for axon.

:class:`AxonConfig` is loaded from YAML and shapes how the orchestrator
behaves across all runs against that config (main model, system prompt,
parallelism, compaction, registries for system prompts and finalize
schemas). :class:`OrchInputs` is per-run (the question, optional
operating assumptions, budget, optional seed artifacts).
:class:`OrchResult` is what the orchestrator returns when finalize
fires.

Operating assumptions are folded into the artifact store at the
reserved key ``"operating_assumptions"`` rather than wired through a
separate inheritance flag — see :func:`build_initial_artifacts`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rumil.orchestrators.axon.artifacts import ArtifactSeed

OPERATING_ASSUMPTIONS_KEY = "operating_assumptions"


@dataclass(frozen=True)
class AxonConfig:
    """Run-level config for the axon orchestrator.

    Static across runs against this config — the per-run dynamics live
    on :class:`OrchInputs`.
    """

    name: str
    main_model: str
    main_system_prompt_path: str | Path
    max_parallel_delegates_per_turn: int = 4
    hard_max_rounds: int = 50
    # Cap on how many seed_page_ids the spine sees in its first user
    # message. Excess IDs from OrchInputs.seed_page_ids are dropped
    # (with a log warning) so a caller passing an unbounded list
    # doesn't blow the spine's first turn.
    max_seed_pages: int = 20
    # When True (default) and OrchInputs.seed_page_ids is empty, embed
    # the question text and pull top-K similar pages (K=max_seed_pages)
    # via search_pages_by_vector to seed the spine. Caller-supplied
    # seed_page_ids take priority — auto-seed only fires when the
    # caller's list is empty. Disable for runs that intentionally start
    # blank (versus blind judging, isolated tests, brand-new questions
    # with empty workspaces).
    auto_seed_from_question: bool = True
    # Cosine-similarity threshold for auto-seed lookups. Lower values
    # surface more pages but include weaker matches. Mirrors
    # search_pages_by_vector's default; tunable per config.
    auto_seed_match_threshold: float = 0.5

    # Server-side compaction (Anthropic compact_*).
    enable_server_compaction: bool = True
    compaction_trigger_tokens: int = 400_000
    compaction_instructions_path: str | Path | None = None

    # Config-time artifact seeds. Loaded from YAML's `artifact_seeds`
    # block (text inlined or read from a `path:`); folded into the
    # run's ArtifactStore at run start with ``produced_by="input"``,
    # alongside any :class:`OrchInputs.artifacts` from the caller.
    # System prompts that delegates can reference via
    # ``DelegateConfig.system_prompt = {ref: "<key>"}`` live here —
    # the orchestrator resolves the ref against the ArtifactStore, so
    # delegates created mid-run can also write prompt-shaped artifacts
    # for siblings to use. ``render_inline`` defaults to False so the
    # spine sees an announcement + description but not the full body
    # until it calls ``read_artifact``.
    artifact_seeds: Mapping[str, ArtifactSeed] = field(default_factory=dict)
    # JSON schemas referenced by ``DelegateConfig.finalize_schema =
    # {ref: "<name>"}``. Kept separate from artifacts because the
    # consumer needs a dict, not text, and `output_format` on the
    # inner loop's tool expects structured JSON Schema.
    finalize_schema_registry: Mapping[str, dict[str, Any]] = field(default_factory=dict)

    # Direct tool registry — names of tools (web_research, workspace_lookup,
    # etc.) that are exposed to mainline AND available for delegates
    # via DelegateConfig.tools. The universal `finalize` tool is added
    # per inner loop and is not listed here.
    direct_tools: tuple[str, ...] = ()


@dataclass
class OrchInputs:
    """Per-run inputs for an axon orchestrator run."""

    question: str
    budget_usd: float
    operating_assumptions: str = ""
    # Existing workspace pages to surface to the spine at run start.
    # The orchestrator renders id + type + headline (NOT content) for
    # each in the spine's first user message under "## Available pages".
    # The spine calls load_page(id) for full content on demand. Capped
    # at AxonConfig.max_seed_pages — caller-supplied lists longer than
    # the cap are truncated with a log warning.
    seed_page_ids: Sequence[str] = ()
    # Caller-seeded artifacts (run-local text-by-key). Distinct from
    # pages: artifacts are for content that doesn't fit the workspace
    # graph. Folded into the run's ArtifactStore before mainline's
    # first turn. Each entry is either a plain string (full body, no
    # description, not rendered inline — body fetched via the
    # ``read_artifact`` mainline tool) or an :class:`ArtifactSeed`
    # carrying ``text`` + a short ``description`` + a ``render_inline``
    # flag (True ⇒ body XML-fenced into the spine's first user message).
    artifacts: Mapping[str, str | ArtifactSeed] = field(default_factory=dict)
    wall_clock_soft_s: float | None = None


@dataclass
class OrchResult:
    """What :meth:`AxonOrchestrator.run` returns when finalize fires."""

    answer_text: str
    cost_usd_used: float
    rounds_used: int
    last_status: str  # "completed" | "incomplete" | "budget_exhausted"
    run_id: str
    call_id: str


def build_initial_artifacts(
    inputs: OrchInputs,
    config_seeds: Mapping[str, ArtifactSeed] | None = None,
) -> dict[str, str | ArtifactSeed]:
    """Combine config-time seeds, caller seeds, and operating assumptions.

    Layering order (collision raises):

    1. ``config_seeds`` — from :attr:`AxonConfig.artifact_seeds`,
       including prompt-shaped seeds that delegates reference via
       ``DelegateConfig.system_prompt = {ref: "<key>"}``.
    2. ``inputs.artifacts`` — caller-runtime seeds.
    3. ``operating_assumptions`` — landed at the reserved key
       :data:`OPERATING_ASSUMPTIONS_KEY` as a render-inline ArtifactSeed.

    Empty/whitespace-only assumptions are not seeded. Raises on any
    key collision so silent shadowing can't happen.
    """
    seed: dict[str, str | ArtifactSeed] = {}
    if config_seeds:
        for k, v in config_seeds.items():
            seed[k] = v
    for k, v in inputs.artifacts.items():
        if k in seed:
            raise ValueError(f"OrchInputs.artifacts[{k!r}] collides with AxonConfig.artifact_seeds")
        seed[k] = v
    if inputs.operating_assumptions.strip():
        if OPERATING_ASSUMPTIONS_KEY in seed:
            raise ValueError(
                f"reserved key {OPERATING_ASSUMPTIONS_KEY!r} already seeded; "
                "operating_assumptions cannot also be set."
            )
        seed[OPERATING_ASSUMPTIONS_KEY] = ArtifactSeed(
            text=inputs.operating_assumptions.strip(),
            description="caller-supplied operating assumptions / constraints",
            render_inline=True,
        )
    return seed
