"""AxonWorkflow — versus :class:`Workflow` adapter.

``produces_artifact=True``: the orchestrator surfaces its final answer
on ``self.last_artifact`` (the workflow contract). The versus runner
reads it from there and feeds it to the task's ``extract_artifact``.
``question.content`` stays untouched throughout the run.

Constructor knobs:

- ``budget_usd`` — hard USD cost cap; the only thing that terminates a
  run. Same shape as :class:`SimpleSpineWorkflow.budget_usd`.
- ``config_name`` — selects a named axon config from
  ``rumil/orchestrators/axon/configs/<name>.yaml`` via
  :func:`discover_configs` + :func:`load_axon_config`. Versus uses
  ``"essay_continuation"`` for completion runs and ``"judge_pair"`` for
  judging runs.
- ``call_type`` — ``"complete"`` ⇒ ``CallType.VERSUS_COMPLETE``;
  ``"judge"`` ⇒ ``CallType.VERSUS_JUDGE``. Drives both the call_type
  recorded on the run and the shape of ``last_artifact``.
- ``artifacts`` — per-run inline artifacts. For judging:
  ``{"prefix", "essay_a", "essay_b", "rubric"}``. For completion:
  ``{"prefix", "target_length_chars"}``. Each value is wrapped in an
  :class:`ArtifactSeed` with ``render_inline=True`` and the key as the
  description, so the body is XML-fenced into the spine's first user
  message.

The fingerprint folds config_name, the loaded config's main_model, the
budget, and a stable hash of the artifacts dict so editing pair surface
or rubric naturally forks the dedup key. No manual version bump.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Mapping, Sequence

from rumil.database import DB
from rumil.model_config import ModelConfig
from rumil.models import CallType
from rumil.orchestrators.axon.artifacts import ArtifactSeed
from rumil.orchestrators.axon.config import AxonConfig, OrchInputs
from rumil.orchestrators.axon.loader import discover_configs, load_axon_config
from rumil.orchestrators.axon.orchestrator import AxonOrchestrator
from rumil.settings import get_settings
from rumil.tracing.broadcast import Broadcaster


def _sha8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def _hash_artifacts(artifacts: Mapping[str, str]) -> str:
    """Stable hash of (key, sha8(text)) pairs sorted by key."""
    blob = json.dumps(
        sorted((k, _sha8(v)) for k, v in artifacts.items()),
        separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _model_supports_compaction(model: str) -> bool:
    """Anthropic gates ``compact_20260112`` on Opus/Sonnet — Haiku 400s."""
    return "haiku" not in model.lower()


def _resolve_config(config_name: str) -> AxonConfig:
    configs = discover_configs()
    if config_name not in configs:
        available = ", ".join(sorted(configs)) or "<none>"
        raise ValueError(f"axon config {config_name!r} not found; available: {available}")
    return load_axon_config(configs[config_name])


_JUDGE_QUESTION = (
    "Judge the pair of essay continuations against the rubric. "
    "All inputs (prefix, essay_a, essay_b, rubric) are inline artifacts "
    "in this message."
)
_COMPLETE_QUESTION = (
    "Continue the essay from the provided prefix. The prefix (and "
    "optional target length) are inline artifacts in this message."
)


class AxonWorkflow:
    """Versus :class:`Workflow` for axon. See module docstring."""

    name: str = "axon"
    produces_artifact: bool = True
    code_paths: Sequence[str] = (
        "src/rumil/orchestrators/axon/",
        "src/rumil/llm.py",
    )
    relevant_settings: Sequence[str] = ()

    def __init__(
        self,
        *,
        budget_usd: float,
        config_name: str,
        call_type: str = "complete",
        wall_clock_soft_s: float | None = None,
        artifacts: Mapping[str, str] | None = None,
    ) -> None:
        if budget_usd <= 0:
            raise ValueError(f"budget_usd must be > 0, got {budget_usd}")
        if call_type not in ("complete", "judge"):
            raise ValueError(f"call_type must be 'complete' or 'judge', got {call_type!r}")
        self.budget_usd = budget_usd
        self.config_name = config_name
        self.config = _resolve_config(config_name)
        self.call_type_str = call_type
        self.call_type_enum = (
            CallType.VERSUS_COMPLETE if call_type == "complete" else CallType.VERSUS_JUDGE
        )
        self.wall_clock_soft_s = wall_clock_soft_s
        self.artifacts: Mapping[str, str] = dict(artifacts) if artifacts else {}
        self.last_status: str = "complete"
        self.last_artifact: str = ""

    def fingerprint(self) -> Mapping[str, str | int | float | bool | None]:
        return {
            "kind": self.name,
            "call_type": self.call_type_str,
            "config_name": self.config_name,
            "config_main_model": self.config.main_model,
            "config_finalize_schema_ref": self.config.mainline_finalize_schema_ref,
            "budget_usd": self.budget_usd,
            "wall_clock_soft_s": (int(self.wall_clock_soft_s) if self.wall_clock_soft_s else None),
            "artifacts_hash": _hash_artifacts(self.artifacts),
        }

    async def setup(self, db: DB, question_id: str) -> None:
        # No rumil-budget unit consumption inside axon — USD budget is the
        # only hard cap. Seed budget=1 for UI parity with other workflows.
        await db.init_budget(1)

    async def run(
        self,
        db: DB,
        question_id: str,
        broadcaster: Broadcaster | None,
        *,
        model_config: ModelConfig | None = None,
    ) -> None:
        del model_config  # axon owns its own per-stage model config.
        rmo = get_settings().rumil_model_override
        effective_config = dataclasses.replace(self.config, main_model=rmo) if rmo else self.config
        if effective_config.enable_server_compaction and not _model_supports_compaction(
            effective_config.main_model
        ):
            effective_config = dataclasses.replace(effective_config, enable_server_compaction=False)

        question_text = _JUDGE_QUESTION if self.call_type_str == "judge" else _COMPLETE_QUESTION
        seeded = {
            k: ArtifactSeed(text=v, description=k, render_inline=True)
            for k, v in self.artifacts.items()
        }
        inputs = OrchInputs(
            question=question_text,
            budget_usd=self.budget_usd,
            artifacts=seeded,
            wall_clock_soft_s=self.wall_clock_soft_s,
        )
        orch = AxonOrchestrator(db, effective_config, broadcaster=broadcaster)
        result = await orch.run(inputs, call_type=self.call_type_enum)
        self.last_status = result.last_status

        payload = result.answer_payload or {}
        if self.call_type_str == "judge":
            reasoning = str(payload.get("reasoning", "")).strip()
            verdict = str(payload.get("verdict", "")).strip()
            if not verdict:
                # Fall back to answer_text so the closer extraction can
                # still rfind a label (or fail cleanly with a missing
                # one) rather than silently dropping the run's work.
                self.last_artifact = result.answer_text
            else:
                self.last_artifact = f"{reasoning}\n\n{verdict}" if reasoning else verdict
        else:
            continuation = str(payload.get("continuation", "")).strip()
            self.last_artifact = continuation or result.answer_text
