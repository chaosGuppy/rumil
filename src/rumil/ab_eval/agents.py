"""Agent specifications for A/B evaluation."""

from collections.abc import Sequence
from dataclasses import dataclass, field


@dataclass
class ABEvalAgentSpec:
    """Defines one evaluation agent's identity and configuration."""

    name: str
    display_name: str
    prompt_file: str
    extra_tools: Sequence[str] = field(default_factory=list)


EVAL_AGENTS: Sequence[ABEvalAgentSpec] = [
    ABEvalAgentSpec(
        name="grounding",
        display_name="Grounding & Factual Correctness",
        prompt_file="ab-eval-grounding.md",
        extra_tools=["WebSearch"],
    ),
    ABEvalAgentSpec(
        name="subquestion_relevance",
        display_name="Subquestion Relevance",
        prompt_file="ab-eval-subquestion-relevance.md",
    ),
    ABEvalAgentSpec(
        name="consistency",
        display_name="Consistency",
        prompt_file="ab-eval-consistency.md",
    ),
    ABEvalAgentSpec(
        name="research_progress",
        display_name="Research Progress",
        prompt_file="ab-eval-research-progress.md",
    ),
    ABEvalAgentSpec(
        name="general_quality",
        display_name="General Quality",
        prompt_file="ab-eval-general-quality.md",
    ),
]
