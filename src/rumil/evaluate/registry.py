"""Evaluation type + grounding-pipeline registry.

Single source of truth for which ``eval_type`` values exist (default,
grounding, feedback) and which follow-up pipelines can be run against an
evaluation call (grounding, feedback_update). CLI, API, chat, and skills
iterate these registries instead of hardcoding the names.

The evaluation prompt/investigator-prompt filenames also live here so that
the registry gives a complete picture of what each eval type looks like
from the outside.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from rumil.clean.feedback import run_feedback_update
from rumil.clean.grounding import run_grounding_feedback
from rumil.database import DB
from rumil.evaluate.prompt import EVAL_PROMPTS, INVESTIGATOR_TASK_PROMPTS
from rumil.models import Call
from rumil.tracing.broadcast import Broadcaster


@dataclass(frozen=True)
class EvaluationTypeSpec:
    name: str
    description: str
    prompt_file: str
    investigator_prompt_file: str


EVALUATION_TYPES: dict[str, EvaluationTypeSpec] = {
    "default": EvaluationTypeSpec(
        name="default",
        description=(
            "Falsifiable grounding: how well is each claim supported by sources "
            "or by cited reasoning? Default choice for most evaluations."
        ),
        prompt_file=EVAL_PROMPTS["default"],
        investigator_prompt_file=INVESTIGATOR_TASK_PROMPTS["default"],
    ),
    "grounding": EvaluationTypeSpec(
        name="grounding",
        description=(
            "Legacy grounding evaluation. Prefer 'default' unless you need the "
            "older prompt for comparison."
        ),
        prompt_file=EVAL_PROMPTS["grounding"],
        investigator_prompt_file=INVESTIGATOR_TASK_PROMPTS["default"],
    ),
    "feedback": EvaluationTypeSpec(
        name="feedback",
        description=(
            "Holistic feedback: structural issues, framing problems, missing "
            "subquestions. Less about grounding, more about how the research is "
            "shaped."
        ),
        prompt_file=EVAL_PROMPTS["feedback"],
        investigator_prompt_file=INVESTIGATOR_TASK_PROMPTS["feedback"],
    ),
}


def get_evaluation_type_spec(name: str) -> EvaluationTypeSpec:
    spec = EVALUATION_TYPES.get(name)
    if spec is None:
        raise ValueError(f"Unknown eval_type: {name!r}. Available: {sorted(EVALUATION_TYPES)}")
    return spec


GroundingPipelineRunner = Callable[..., Awaitable[Call]]


@dataclass(frozen=True)
class GroundingPipelineSpec:
    name: str
    description: str
    runner: GroundingPipelineRunner
    recommended_eval_type: str


async def _run_grounding(
    question_id: str,
    evaluation_text: str,
    db: DB,
    *,
    broadcaster: Broadcaster | None = None,
    from_stage: int = 1,
    prior_checkpoints: dict | None = None,
) -> Call:
    return await run_grounding_feedback(
        question_id,
        evaluation_text,
        db,
        broadcaster=broadcaster,
        from_stage=from_stage,
        prior_checkpoints=prior_checkpoints,
    )


async def _run_feedback(
    question_id: str,
    evaluation_text: str,
    db: DB,
    *,
    broadcaster: Broadcaster | None = None,
    from_stage: int = 1,
    prior_checkpoints: dict | None = None,
) -> Call:
    return await run_feedback_update(
        question_id,
        evaluation_text,
        db,
        broadcaster=broadcaster,
        from_stage=from_stage,
        prior_checkpoints=prior_checkpoints,
    )


GROUNDING_PIPELINES: dict[str, GroundingPipelineSpec] = {
    "grounding": GroundingPipelineSpec(
        name="grounding",
        description=(
            "Web-research the grounding gaps surfaced by a default/grounding "
            "evaluation and plan+apply updates to the claims. Five-stage pipeline."
        ),
        runner=_run_grounding,
        recommended_eval_type="default",
    ),
    "feedback": GroundingPipelineSpec(
        name="feedback",
        description=(
            "Apply the structural edits (split/merge/reframe questions, update "
            "framing) recommended by a feedback evaluation. Three-stage pipeline."
        ),
        runner=_run_feedback,
        recommended_eval_type="feedback",
    ),
}


def get_grounding_pipeline_spec(name: str) -> GroundingPipelineSpec:
    spec = GROUNDING_PIPELINES.get(name)
    if spec is None:
        raise ValueError(
            f"Unknown grounding pipeline: {name!r}. Available: {sorted(GROUNDING_PIPELINES)}"
        )
    return spec
