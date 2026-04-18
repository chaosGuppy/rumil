"""Assess-on-creation triage for newly-created research sub-questions.

The triage is a cheap Haiku-based structured call that runs right after a
question is created. It produces a ``TriageVerdict`` covering fertility,
duplicate detection, ill-posedness, and scope appropriateness. The verdict is
stored on the question's ``extra["triage"]`` field — metadata only, no routing
or prioritization behavior is keyed off it here. Downstream consumers
(prioritization, UI filters) can read it.

Gated behind ``settings.enable_question_triage`` (default False). Failures are
swallowed so question creation is never blocked by the triage call.
"""

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from rumil.embeddings import embed_query, search_pages_by_vector
from rumil.llm import structured_call
from rumil.models import Page, PageType, Workspace
from rumil.settings import get_settings

if TYPE_CHECKING:
    from rumil.database import DB

log = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"

TRIAGE_VERSION = 1


class TriageVerdict(BaseModel):
    """Structured verdict from the question triage LLM call."""

    fertility_score: int = Field(description="1-5, how much to invest (5=priority)")
    is_duplicate: bool = Field(description="Is this semantically already covered?")
    duplicate_of: str | None = Field(
        default=None,
        description="Full UUID of the likely duplicate, if any",
    )
    is_ill_posed: bool = Field(description="Does the question presume something false?")
    ill_posed_reason: str = Field(default="", description="If ill_posed: why, briefly")
    scope_appropriate: bool = Field(description="Does this fit under the parent's scope?")
    scope_reason: str = Field(default="", description="If not scope_appropriate: why")
    reasoning: str = Field(description="One-paragraph rationale for the verdict")

    def to_payload(self) -> dict:
        """Return the JSONB payload stored in ``pages.extra['triage']``."""
        return {
            "fertility_score": self.fertility_score,
            "is_duplicate": self.is_duplicate,
            "duplicate_of": self.duplicate_of,
            "is_ill_posed": self.is_ill_posed,
            "ill_posed_reason": self.ill_posed_reason,
            "scope_appropriate": self.scope_appropriate,
            "scope_reason": self.scope_reason,
            "reasoning": self.reasoning,
            "triaged_at": datetime.now(UTC).isoformat(),
            "triage_version": TRIAGE_VERSION,
        }


def _triage_system_prompt() -> str:
    return (_PROMPTS_DIR / "question_triage.md").read_text()


def _format_neighbor(page: Page, similarity: float, prelabel_duplicate: bool) -> str:
    parts = [
        f"- id: `{page.id}` (similarity {similarity:.2f})",
        f"  headline: {page.headline}",
    ]
    if page.abstract:
        abstract = page.abstract.strip().replace("\n", " ")
        if len(abstract) > 240:
            abstract = abstract[:240] + "…"
        parts.append(f"  abstract: {abstract}")
    if prelabel_duplicate:
        parts.append("  NOTE: similarity above auto-duplicate threshold — likely duplicate.")
    return "\n".join(parts)


def _build_user_message(
    question_headline: str,
    question_abstract: str | None,
    parent_question: Page | None,
    neighbors: list[tuple[Page, float]],
    auto_duplicate_threshold: float,
) -> str:
    sections: list[str] = []

    parts = [
        "## The question to triage",
        f"Headline: {question_headline}",
    ]
    if question_abstract and question_abstract.strip():
        parts.append(f"\nAbstract / context:\n{question_abstract.strip()}")
    sections.append("\n".join(parts))

    if parent_question is not None:
        parent_parts = [
            "## Parent question",
            f"Headline: {parent_question.headline}",
        ]
        if parent_question.abstract and parent_question.abstract.strip():
            parent_parts.append(f"\nAbstract:\n{parent_question.abstract.strip()}")
        sections.append("\n".join(parent_parts))
    else:
        sections.append("## Parent question\n(none — this is a root question)")

    if neighbors:
        lines = ["## Embedding-neighbors already in the workspace"]
        for page, similarity in neighbors:
            prelabel = similarity >= auto_duplicate_threshold
            lines.append(_format_neighbor(page, similarity, prelabel))
        sections.append("\n".join(lines))
    else:
        sections.append(
            "## Embedding-neighbors already in the workspace\n(none above similarity floor)"
        )

    sections.append("Triage this question now.")
    return "\n\n".join(sections)


async def _fetch_neighbors(
    db: "DB",
    question_headline: str,
    question_abstract: str | None,
    exclude_ids: set[str],
) -> list[tuple[Page, float]]:
    settings = get_settings()
    query_text = question_headline
    if question_abstract and question_abstract.strip():
        query_text = f"{question_headline}\n\n{question_abstract.strip()}"
    embedding = await embed_query(query_text)
    fetch_count = settings.question_triage_neighbor_count + len(exclude_ids) + 1
    matches = await search_pages_by_vector(
        db,
        embedding,
        match_threshold=settings.question_triage_neighbor_threshold,
        match_count=fetch_count,
        workspace=Workspace.RESEARCH,
    )
    neighbors: list[tuple[Page, float]] = []
    for page, similarity in matches:
        if page.id in exclude_ids:
            continue
        if page.page_type != PageType.QUESTION:
            continue
        neighbors.append((page, similarity))
        if len(neighbors) >= settings.question_triage_neighbor_count:
            break
    return neighbors


async def triage_question(
    db: "DB",
    question_headline: str,
    question_abstract: str | None,
    parent_question: Page | None,
    *,
    question_id: str | None = None,
    model: str | None = None,
) -> TriageVerdict:
    """Call Haiku to produce a ``TriageVerdict`` for a newly-created question.

    Fetches the workspace's embedding-neighbors of the question (top
    ``settings.question_triage_neighbor_count`` above the similarity floor) and
    includes them in the prompt so the model can spot duplicates. If
    ``question_id`` is provided, it (and the parent's id, if any) is excluded
    from the neighbor list.
    """
    settings = get_settings()
    exclude_ids: set[str] = set()
    if question_id:
        exclude_ids.add(question_id)
    if parent_question is not None:
        exclude_ids.add(parent_question.id)

    neighbors = await _fetch_neighbors(
        db,
        question_headline,
        question_abstract,
        exclude_ids,
    )
    user_message = _build_user_message(
        question_headline,
        question_abstract,
        parent_question,
        neighbors,
        settings.question_triage_auto_duplicate_threshold,
    )

    result = await structured_call(
        system_prompt=_triage_system_prompt(),
        user_message=user_message,
        response_model=TriageVerdict,
        model=model or "claude-haiku-4-5-20251001",
    )
    if result.parsed is None:
        raise RuntimeError("question_triage returned no parsed output")
    return result.parsed


async def auto_triage_and_save(
    db: "DB",
    question_id: str,
    parent_id: str | None,
) -> dict | None:
    """Run the triage on a newly-created question and persist the verdict.

    Gated on ``settings.enable_question_triage`` (default False). Swallows all
    exceptions so a triage failure can never break question creation. Returns
    the payload written to ``extra["triage"]`` or ``None`` if skipped/failed.
    """
    if not get_settings().enable_question_triage:
        return None

    try:
        question = await db.get_page(question_id)
        if question is None:
            log.warning("auto_triage_and_save: question %s not found", question_id[:8])
            return None
        parent: Page | None = None
        if parent_id:
            parent = await db.get_page(parent_id)
        verdict = await triage_question(
            db,
            question.headline,
            question.abstract,
            parent,
            question_id=question.id,
        )
    except Exception:
        log.warning(
            "question_triage failed for page %s",
            question_id[:8],
            exc_info=True,
        )
        return None

    payload = verdict.to_payload()
    try:
        await db.merge_page_extra(question_id, {"triage": payload})
    except Exception:
        log.warning(
            "Persisting question_triage failed for page %s",
            question_id[:8],
            exc_info=True,
        )
        return None
    log.info(
        "question_triage saved: page=%s fertility=%d duplicate=%s",
        question_id[:8],
        verdict.fertility_score,
        verdict.is_duplicate,
    )
    return payload
