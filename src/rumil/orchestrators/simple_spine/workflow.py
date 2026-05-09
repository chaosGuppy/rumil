"""SimpleSpineWorkflow — versus :class:`Workflow` adapter.

``produces_artifact=True``: the orchestrator surfaces its final answer
on ``self.last_artifact`` (the workflow contract). The versus runner
reads it from there and feeds it to the task's ``extract_artifact``.
``question.content`` stays untouched throughout the run.

Constructor knobs:

- ``config`` — the :class:`SimpleSpineConfig` instance (the variant under test).
- ``call_type`` — distinguishes essay-continuation runs (``VERSUS_COMPLETE``)
  from judging runs (``VERSUS_JUDGE``) so analytics stay clean.
- ``max_tokens`` — hard token cap (the only hard cap in SimpleSpine).
- ``wall_clock_soft_s`` — surfaced to the agent as a soft signal.
- ``operating_assumptions`` / ``output_guidance`` / ``additional_context``
  — freeform strings spliced into the initial mainline user message.

The fingerprint folds in the config's fingerprint plus content hashes
of the freeform strings, so editing any of them auto-forks the dedup
key — no manual version bump.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Mapping, Sequence

from rumil.database import DB
from rumil.model_config import ModelConfig
from rumil.models import CallType
from rumil.orchestrators.simple_spine.budget_clock import BudgetSpec
from rumil.orchestrators.simple_spine.config import OrchInputs, SimpleSpineConfig
from rumil.orchestrators.simple_spine.orchestrator import SimpleSpineOrchestrator
from rumil.orchestrators.simple_spine.subroutines.base import sha8
from rumil.settings import get_settings
from rumil.tracing.broadcast import Broadcaster


def _hash_artifacts(artifacts: Mapping[str, str]) -> str:
    """Stable hash of (key, sha8(text)) pairs sorted by key.

    Used to fold caller-seeded ``OrchInputs.artifacts`` into the
    workflow fingerprint so an edit to pair surface / rubric forks the
    versus dedup hash. Keys and content-hashes both go in — a renamed
    key with the same body forks; a same-named key with a different
    body forks too.
    """
    blob = json.dumps(
        sorted((k, sha8(v)) for k, v in artifacts.items()),
        separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _apply_model_override(config: SimpleSpineConfig, model: str) -> SimpleSpineConfig:
    """Return a copy of ``config`` with every role-model replaced by ``model``.

    Walks the subroutine library and replaces ``model`` on any subroutine
    that has it (FreeformAgent, SampleN); subroutines without a ``model``
    field (CallType, NestedOrch) pass through unchanged. Also overrides
    the orch's ``main_model``. Versus passes its ``--model`` flag via
    ``override_settings(rumil_model_override=...)`` before calling
    workflow.run; we honor that here so a single CLI knob governs which
    model every internal LLM call uses, while keeping configs as the
    canonical template that pins per-role models when desired.
    """
    new_library = []
    for sub in config.process_library:
        if dataclasses.is_dataclass(sub) and hasattr(sub, "model"):
            new_library.append(dataclasses.replace(sub, model=model))
        else:
            new_library.append(sub)
    return dataclasses.replace(config, main_model=model, process_library=tuple(new_library))


class SimpleSpineWorkflow:
    """Versus :class:`Workflow` for SimpleSpine. See module docstring."""

    name: str = "simple_spine"
    produces_artifact: bool = True
    code_paths: Sequence[str] = (
        "src/rumil/orchestrators/simple_spine/",
        "src/rumil/llm.py",
    )
    relevant_settings: Sequence[str] = ()

    def __init__(
        self,
        *,
        budget_tokens: int,
        config_name: str = "default",
        call_type: str = "complete",
        wall_clock_soft_s: float | None = None,
        operating_assumptions: str = "",
        output_guidance: str = "",
        additional_context: str = "",
        artifacts: Mapping[str, str] | None = None,
        **deprecated_kwargs: object,
    ) -> None:
        """Construct a SimpleSpine workflow.

        ``budget_tokens`` is the raw token cap on the run — the only
        thing that terminates it. SimpleSpine's budget primitive is
        tokens, not the rumil "budget unit" used by other orchs (one
        rumil call vs. one shared token pool), so we keep the names
        distinct: pass `budget_tokens=200_000` for a 200k-token cap.

        ``config_name`` resolves a named :class:`SimpleSpineConfig` via
        :func:`rumil.orchestrators.simple_spine.presets.get_preset`.
        ``call_type`` is one of ``"complete"`` / ``"judge"`` and selects
        the rumil ``CallType`` recorded on the orch's call row.
        """
        from rumil.orchestrators.simple_spine.presets import get_preset

        if "budget" in deprecated_kwargs or "tokens_per_round" in deprecated_kwargs:
            raise TypeError(
                "SimpleSpineWorkflow no longer accepts `budget` / "
                "`tokens_per_round`; pass `budget_tokens=<int>` directly. "
                "The unit-based budget (budget × tokens_per_round) was "
                "removed because SimpleSpine's hard cap is a token count, "
                "unlike other orchs whose `budget` counts research calls."
            )
        if deprecated_kwargs:
            raise TypeError(f"unexpected kwargs: {sorted(deprecated_kwargs)}")
        if budget_tokens < 1000:
            raise ValueError(f"budget_tokens must be >= 1000, got {budget_tokens}")
        if call_type not in ("complete", "judge"):
            raise ValueError(f"call_type must be 'complete' or 'judge', got {call_type!r}")
        self.budget_tokens = budget_tokens
        self.max_tokens = budget_tokens
        self.config_name = config_name
        self.config = get_preset(config_name)
        self.call_type_str = call_type
        self.call_type_enum = (
            CallType.VERSUS_COMPLETE if call_type == "complete" else CallType.VERSUS_JUDGE
        )
        self.wall_clock_soft_s = wall_clock_soft_s
        self.operating_assumptions = operating_assumptions
        self.output_guidance = output_guidance
        self.additional_context = additional_context
        self.artifacts: Mapping[str, str] = dict(artifacts) if artifacts else {}
        self.last_status: str = "complete"
        # Set in run() to the orch's answer_text; the versus runner reads
        # this directly when produces_artifact=True.
        self.last_artifact: str = ""

    def fingerprint(self) -> Mapping[str, str | int | bool | None]:
        return {
            "kind": self.name,
            "call_type": self.call_type_str,
            "config_name": self.config_name,
            "config_fingerprint": self.config.fingerprint,
            "budget_tokens": self.budget_tokens,
            "wall_clock_soft_s": (int(self.wall_clock_soft_s) if self.wall_clock_soft_s else None),
            "operating_assumptions_hash": sha8(self.operating_assumptions),
            "output_guidance_hash": sha8(self.output_guidance),
            "additional_context_hash": sha8(self.additional_context),
            # Caller-seeded artifacts are inputs (per-pair pair_text /
            # rubric for versus, etc.). Folding key+content hashes into
            # the workflow fingerprint means an edit to pair surface or
            # rubric naturally forks the dedup hash; not folding them
            # would let two judgments at different rubrics collide.
            "artifacts_hash": _hash_artifacts(self.artifacts),
        }

    async def setup(self, db: DB, question_id: str) -> None:
        # No rumil-budget unit consumption inside SimpleSpine — token
        # budget is the only hard cap. Seed budget=1 just for telemetry
        # parity with other workflows that show a budget total in the UI.
        await db.init_budget(1)

    async def run(
        self,
        db: DB,
        question_id: str,
        broadcaster: Broadcaster | None,
        *,
        model_config: ModelConfig | None = None,
    ) -> None:
        # ``model_config`` is intentionally not threaded through; SimpleSpine
        # owns its own per-stage ModelConfig.
        del model_config
        # Honor versus's ``--model`` (set on settings.rumil_model_override
        # by run_versus before calling us) by overriding every role-model
        # in the config. Configs stay the canonical template; the runtime
        # override is applied as a one-knob global.
        rmo = get_settings().rumil_model_override
        effective_config = _apply_model_override(self.config, rmo) if rmo else self.config
        inputs = OrchInputs(
            question_id=question_id,
            additional_context=self.additional_context,
            operating_assumptions=self.operating_assumptions,
            output_guidance=self.output_guidance,
            output_schema=None,
            budget=BudgetSpec(
                max_tokens=self.max_tokens,
                wall_clock_soft_s=self.wall_clock_soft_s,
            ),
            artifacts=dict(self.artifacts),
        )
        orch = SimpleSpineOrchestrator(db, effective_config, broadcaster=broadcaster)
        result = await orch.run(inputs, call_type=self.call_type_enum)
        self.last_status = result.last_status
        # Surface the deliverable via the workflow contract — the versus
        # runner reads ``last_artifact`` directly. We deliberately do not
        # mutate question.content: it stays as the input (pair surface
        # for judging, prefix-framing for completion) so handlers
        # observing the workspace mid-run or post-run see what the
        # workflow consumed, not what it produced.
        self.last_artifact = result.answer_text
