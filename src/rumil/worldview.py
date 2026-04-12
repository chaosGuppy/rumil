"""
Generate a worldview — a hierarchical, importance-ordered summary of research on a question.

The worldview is a tree where depth = centrality/importance, not category.
L0 nodes are the most important things to know. Each node can be any type
(claim, hypothesis, evidence, uncertainty, context). Children elaborate,
support, or qualify their parent.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel

from rumil.database import DB
from rumil.llm import structured_call
from rumil.summary import build_research_tree


PROMPTS_DIR = __import__("pathlib").Path(__file__).parent.parent.parent / "prompts"


class WorldviewNode(BaseModel):
    node_type: Literal["claim", "hypothesis", "evidence", "uncertainty", "context"]
    headline: str
    content: str
    credence: int | None = None
    robustness: int | None = None
    source_page_ids: Sequence[str] = []
    children: Sequence["WorldviewNode"] = []


class Worldview(BaseModel):
    question_id: str
    question_headline: str
    summary: str
    nodes: Sequence[WorldviewNode]
    generated_at: str


async def generate_worldview(
    question_id: str,
    db: DB,
    *,
    max_depth: int = 4,
) -> Worldview:
    question = await db.get_page(question_id)
    if not question:
        raise ValueError(f"Question {question_id} not found")

    research_tree = await build_research_tree(question_id, db, max_depth=max_depth)
    if not research_tree.strip():
        return Worldview(
            question_id=question_id,
            question_headline=question.headline,
            summary="No research found yet.",
            nodes=[],
            generated_at=datetime.now(UTC).isoformat(),
        )

    system_prompt = (PROMPTS_DIR / "worldview.md").read_text(encoding="utf-8")
    user_message = (
        f"# Question: {question.headline}\n\n"
        f"## Full research tree\n\n"
        f"{research_tree}\n\n"
        "---\n\n"
        "Synthesize the above research into a worldview tree."
    )

    result = await structured_call(
        system_prompt=system_prompt,
        user_message=user_message,
        response_model=Worldview,
    )

    if not result.parsed:
        raise RuntimeError(f"Failed to generate worldview: {result.response_text}")

    worldview = result.parsed
    worldview.question_id = question_id
    worldview.question_headline = question.headline
    worldview.generated_at = datetime.now(UTC).isoformat()
    return worldview
