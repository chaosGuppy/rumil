"""Build the system prompt for the evaluation agent."""

from pathlib import Path

_PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"


EVAL_PROMPTS: dict[str, str] = {
    "default": "eval-falsifiable-grounding.md",
    "grounding": "evaluate.md",
    "feedback": "eval-feedback.md",
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

    if eval_type == "feedback":
        task_section = (
            "# Investigator Task\n\n"
            "You are an investigator subagent. You have been given a specific "
            "page or area of the research graph to explore. Your ONLY job is to "
            "explore the graph thoroughly and report what you find — the parent "
            "agent will interpret your findings.\n\n"
            "Use the `explore_page` tool to navigate outward from the starting "
            "page. Follow links to subquestions, considerations, claims, "
            "judgements, and sources.\n\n"
            "Report back with a concise factual summary:\n\n"
            "1. What subquestions, considerations, and claims exist in this "
            "area of the graph (cite page headlines WITH their 8-char short "
            "IDs, e.g. [abcd1234] 'Solar payback claim')\n"
            "2. How developed each branch is — does it have depth "
            "(multiple levels of subquestions, supporting evidence) or is it "
            "thin (a single claim with no further support)?\n"
            "3. Any apparent contradictions between pages you encounter — "
            "claims or judgements that seem to conflict with each other\n"
            "4. Dead ends or abandoned branches — subquestions with no "
            "judgement, considerations with no supporting evidence\n\n"
            "Do NOT produce an overall evaluation or assessment. Do NOT "
            "suggest improvements. Just report what is and is not in the "
            "graph. The parent agent will make the judgement."
        )
    else:
        task_section = (
            "# Investigator Task\n\n"
            "You are an investigator subagent. You have been given a specific page "
            "or claim to trace through the research workspace. Your ONLY job is to "
            "explore the graph and report what you find — the parent agent will "
            "interpret your findings.\n\n"
            "Use the `explore_page` tool to navigate outward from the starting "
            "page. Follow links to considerations, sources, and sub-questions.\n\n"
            "Report back with a concise factual summary:\n\n"
            "1. What pages you found that support or undermine the claim "
            "(cite page headlines WITH their 8-char short IDs, "
            "e.g. [abcd1234] 'Solar payback claim')\n"
            "2. Whether the evidence chain reaches actual Source pages\n"
            "3. Where the chain breaks — missing links, dead ends, "
            "circular references\n\n"
            "Do NOT produce an overall evaluation or assessment. Do NOT rate "
            "the grounding quality. Just report what is and is not in the graph. "
            "The parent agent will make the judgement."
        )

    return (
        preamble + "\n\n" + task_section + "\n\n"
        "## Handling Large Outputs\n\n"
        "Tool outputs sometimes exceed the Read tool's size limit and get "
        "saved to a file. When this happens:\n\n"
        "- Use `Read` with `offset` and `limit` parameters to read the file "
        "in sections rather than attempting to read it all at once.\n"
        "- Use `Grep` to search within the saved file for specific page IDs, "
        "headlines, or keywords rather than reading the entire file.\n"
        "- Do not give up on large outputs — the information is still "
        "accessible, you just need to retrieve it in parts."
    )
