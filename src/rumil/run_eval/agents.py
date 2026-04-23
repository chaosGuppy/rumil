"""Agent specifications for run evaluation."""

from collections.abc import Sequence
from dataclasses import dataclass, field


@dataclass
class EvalAgentSpec:
    """Defines one evaluation agent's identity and configuration."""

    name: str
    display_name: str
    prompt_file: str
    extra_tools: Sequence[str] = field(default_factory=list)


EVAL_AGENTS: Sequence[EvalAgentSpec] = [
    EvalAgentSpec(
        name="grounding",
        display_name="Grounding & Factual Correctness",
        prompt_file="run-eval-grounding.md",
        extra_tools=["WebSearch"],
    ),
    EvalAgentSpec(
        name="coverage_and_relevance",
        display_name="Coverage & Relevance",
        prompt_file="run-eval-coverage-and-relevance.md",
    ),
    EvalAgentSpec(
        name="depth_vs_breadth",
        display_name="Depth vs Breadth",
        prompt_file="run-eval-depth-vs-breadth.md",
    ),
    EvalAgentSpec(
        name="research_redundancy",
        display_name="Research Redundancy",
        prompt_file="run-eval-research-redundancy.md",
    ),
    EvalAgentSpec(
        name="consistency",
        display_name="Consistency",
        prompt_file="run-eval-consistency.md",
    ),
    EvalAgentSpec(
        name="research_progress",
        display_name="Research Progress",
        prompt_file="run-eval-research-progress.md",
    ),
    EvalAgentSpec(
        name="general_quality",
        display_name="General Quality",
        prompt_file="run-eval-general-quality.md",
    ),
]
