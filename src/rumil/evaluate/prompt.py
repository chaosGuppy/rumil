"""Build the system prompt for the evaluation agent."""

from pathlib import Path

_PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"


def build_evaluation_prompt() -> str:
    """Concatenate preamble.md and evaluate.md into a single system prompt."""
    preamble = (_PROMPTS_DIR / "preamble.md").read_text()
    evaluate = (_PROMPTS_DIR / "evaluate.md").read_text()
    return preamble + "\n\n" + evaluate


def build_investigator_prompt() -> str:
    """Build the system prompt for the investigator subagent."""
    preamble = (_PROMPTS_DIR / "preamble.md").read_text()
    return (
        preamble + "\n\n"
        "# Investigator Task\n\n"
        "You are an investigator subagent. You have been given a specific page "
        "or claim to trace through the research workspace. Your ONLY job is to "
        "explore the graph and report what you find — the parent agent will "
        "interpret your findings.\n\n"
        "Use the `explore_page` tool to navigate outward from the starting "
        "page. Follow links to considerations, sources, and sub-questions.\n\n"
        "Report back with a concise factual summary:\n\n"
        "1. What pages you found that support or undermine the claim "
        "(cite page IDs)\n"
        "2. Whether the evidence chain reaches actual Source pages\n"
        "3. Where the chain breaks — missing links, dead ends, "
        "circular references\n\n"
        "Do NOT produce an overall evaluation or assessment. Do NOT rate "
        "the grounding quality. Just report what is and is not in the graph. "
        "The parent agent will make the judgement."
    )
