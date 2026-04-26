"""System prompt builder for the scope-subquestion linker agent."""

from rumil.prompts import PROMPTS_DIR as _PROMPTS_DIR


def build_linker_prompt(max_rounds: int) -> str:
    """Build the system prompt for the subquestion-linker agent."""
    preamble = (_PROMPTS_DIR / "preamble.md").read_text()
    body = (_PROMPTS_DIR / "scope_subquestion_linker.md").read_text()
    return preamble + "\n\n" + body.replace("{max_rounds}", str(max_rounds))
