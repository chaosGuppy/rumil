"""Versus tasks: pluggable per-task half of the universal versus runner.

Each task implements the :class:`VersusTask` protocol and is plugged
into :func:`rumil.versus_runner.run_versus` together with a workflow
(see :mod:`rumil.versus_workflow`).
"""

from __future__ import annotations

from versus.tasks.base import TArtifact, TInputs, VersusTask
from versus.tasks.complete_essay import (
    CompleteEssayTask,
    CompletionArtifact,
    EssayPrefixContext,
    compute_completion_closer_hash,
    compute_question_surface_hash,
)
from versus.tasks.judge_pair import (
    JudgeArtifact,
    JudgePairTask,
    PairContext,
    compute_closer_hash,
    compute_pair_surface_hash,
    compute_tool_prompt_hash,
)

__all__ = (
    "CompleteEssayTask",
    "CompletionArtifact",
    "EssayPrefixContext",
    "JudgeArtifact",
    "JudgePairTask",
    "PairContext",
    "TArtifact",
    "TInputs",
    "VersusTask",
    "compute_closer_hash",
    "compute_completion_closer_hash",
    "compute_pair_surface_hash",
    "compute_question_surface_hash",
    "compute_tool_prompt_hash",
)
