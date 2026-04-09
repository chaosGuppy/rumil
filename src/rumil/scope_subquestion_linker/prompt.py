"""System prompt builder for the scope-subquestion linker agent."""

from pathlib import Path

_PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"


def build_linker_prompt(max_rounds: int) -> str:
    """Build the system prompt for the subquestion-linker agent."""
    preamble = (_PROMPTS_DIR / "preamble.md").read_text()
    body = (_PROMPTS_DIR / "scope_subquestion_linker.md").read_text()
    return preamble + "\n\n" + body.replace("{max_rounds}", str(max_rounds))
