"""
Generate a human-readable executive summary of research on a question.
"""
from datetime import datetime
from pathlib import Path

from database import DB
from llm import run_llm
from models import PageType

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"
SUMMARIES_DIR = Path(__file__).parent.parent / "pages" / "summaries"


def _load_prompt_file(name: str) -> str:
    path = PROMPTS_DIR / name
    return path.read_text(encoding="utf-8") if path.exists() else ""


def build_research_tree(question_id: str, db: DB, depth: int = 0, max_depth: int = 4) -> str:
    """
    Recursively build a full picture of the research on a question:
    the question itself, all its considerations (with full content),
    all its judgements, and all sub-questions (recursively).
    """
    question = db.get_page(question_id)
    if not question:
        return ""

    indent = "  " * depth
    parts = []

    # Question heading
    if depth == 0:
        parts.append(f"# Research Question\n\n{question.content}\n")
    else:
        parts.append(f"{'#' * (depth + 2)} Sub-question: {question.summary}\n\n{question.content}\n")

    # Considerations
    considerations = db.get_considerations_for_question(question_id)
    if considerations:
        supports = [(p, l) for p, l in considerations if l.direction and l.direction.value == "supports"]
        opposes  = [(p, l) for p, l in considerations if l.direction and l.direction.value == "opposes"]
        neutral  = [(p, l) for p, l in considerations if not l.direction or l.direction.value == "neutral"]

        def format_consideration(claim, link) -> str:
            strength_note = f" (strength {link.strength:.1f})" if link.strength else ""
            lines = [f"- **{claim.summary}**{strength_note}"]
            lines.append(f"  {claim.content}")
            if link.reasoning:
                lines.append(f"  *Bearing on question: {link.reasoning}*")
            return "\n".join(lines)

        if supports:
            parts.append(f"{indent}**Supporting considerations:**\n")
            for p, l in sorted(supports, key=lambda x: x[1].strength, reverse=True):
                parts.append(format_consideration(p, l))
            parts.append("")

        if opposes:
            parts.append(f"{indent}**Opposing considerations:**\n")
            for p, l in sorted(opposes, key=lambda x: x[1].strength, reverse=True):
                parts.append(format_consideration(p, l))
            parts.append("")

        if neutral:
            parts.append(f"{indent}**Other relevant considerations:**\n")
            for p, l in neutral:
                parts.append(format_consideration(p, l))
            parts.append("")

    # Judgements — oldest first so the evolution of thinking is legible
    judgements = db.get_judgements_for_question(question_id)
    if judgements:
        import json as _json
        ordered = sorted(judgements, key=lambda j: j.created_at)
        for i, j in enumerate(ordered):
            label = f"Judgement {i + 1} of {len(ordered)}" if len(ordered) > 1 else "Judgement"
            parts.append(f"{indent}**{label}** (confidence {j.epistemic_status:.2f} — {j.epistemic_type}):\n")
            parts.append(j.content)
            extra = _json.loads(j.extra) if j.extra else {}
            if extra.get("key_dependencies"):
                parts.append(f"\n*Key dependencies: {extra['key_dependencies']}*")
            if extra.get("sensitivity_analysis"):
                parts.append(f"\n*Sensitivity: {extra['sensitivity_analysis']}*")
            parts.append("")

    # Sub-questions (recurse)
    if depth < max_depth:
        children = db.get_child_questions(question_id)
        if children:
            for child in children:
                parts.append(build_research_tree(child.id, db, depth=depth + 1, max_depth=max_depth))

    return "\n".join(parts)


def generate_summary(question_id: str, db: DB) -> str:
    """
    Generate an executive summary of research on a question.
    Returns the summary as a string.
    """
    question = db.get_page(question_id)
    if not question:
        raise ValueError(f"Question {question_id} not found")

    research_tree = build_research_tree(question_id, db)

    if not research_tree.strip():
        return f"No research found for question: {question.summary}"

    system_prompt = _load_prompt_file("summarise.md")
    closing = _load_prompt_file("summarise-closing.md")
    user_message = (
        f"Here is the full body of research on this question:\n\n"
        f"{research_tree}\n\n"
        f"---\n\n"
        f"{closing}"
    )

    return run_llm(system_prompt=system_prompt, user_message=user_message, max_tokens=8192)


def save_summary(summary_text: str, question_summary: str) -> Path:
    """Write the summary to a file and return the path."""
    SUMMARIES_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    slug = "".join(c if c.isalnum() or c in " -" else "" for c in question_summary[:50])
    slug = slug.strip().replace(" ", "-").lower()
    filename = f"{timestamp}-{slug}.md"
    path = SUMMARIES_DIR / filename
    path.write_text(summary_text, encoding="utf-8")
    return path
