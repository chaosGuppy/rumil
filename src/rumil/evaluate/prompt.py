"""Build the system prompt for the evaluation agent."""

from pathlib import Path

_PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"


EVAL_PROMPTS: dict[str, str] = {
    "default": "eval-falsifiable-grounding.md",
    "grounding": "evaluate.md",
    "feedback": "eval-feedback.md",
}

INVESTIGATOR_TASK_PROMPTS: dict[str, str] = {
    "feedback": "investigator-feedback.md",
    "default": "investigator-default.md",
}

DEFAULT_EVAL_TYPE = "default"


def build_evaluation_prompt(eval_type: str = DEFAULT_EVAL_TYPE) -> str:
    """Concatenate preamble.md and the chosen evaluation prompt."""
    filename = EVAL_PROMPTS.get(eval_type)
    if filename is None:
        raise ValueError(
            f"Unknown eval type {eval_type!r}. Valid types: {', '.join(EVAL_PROMPTS)}"
        )
    preamble = (_PROMPTS_DIR / "preamble.md").read_text()
    evaluate = (_PROMPTS_DIR / filename).read_text()
    return preamble + "\n\n" + evaluate


def build_investigator_prompt(eval_type: str = DEFAULT_EVAL_TYPE) -> str:
    """Build the system prompt for the investigator subagent."""
    preamble = (_PROMPTS_DIR / "preamble.md").read_text()
    task_filename = INVESTIGATOR_TASK_PROMPTS.get(
        eval_type, INVESTIGATOR_TASK_PROMPTS["default"]
    )
    task_section = (_PROMPTS_DIR / task_filename).read_text()
    large_outputs = (_PROMPTS_DIR / "investigator-large-outputs.md").read_text()
    return preamble + "\n\n" + task_section + "\n\n" + large_outputs
