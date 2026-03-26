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
        "to explore in the research workspace. Your task is to trace the "
        "evidential support for this page by navigating the graph using the "
        "`explore_page` tool.\n\n"
        "Explore outward from the starting page. Follow links to "
        "considerations, sources, and sub-questions. Report back with:\n\n"
        "1. What evidence supports or undermines the page's claims\n"
        "2. Whether the evidence traces back to credible sources\n"
        "3. Any gaps in the chain of justification\n\n"
        "Be specific: cite page IDs and describe what you found at each step."
    )
