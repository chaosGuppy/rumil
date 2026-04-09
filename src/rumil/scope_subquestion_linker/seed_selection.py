"""Select the most relevant top-level human-authored questions to seed the linker agent."""

import logging

from pydantic import BaseModel, Field

from rumil.database import DB
from rumil.llm import structured_call
from rumil.models import Page
from rumil.settings import get_settings

log = logging.getLogger(__name__)


class TopLevelRelevanceRanking(BaseModel):
    selected_short_ids: list[str] = Field(
        description=(
            "Short IDs (8-char prefixes) of the most relevant top-level questions, "
            "in order of relevance. Length must be at most the requested limit."
        )
    )


_SYSTEM_PROMPT = (
    "You are helping a researcher identify which top-level questions in a research "
    "workspace are most likely to contain (or themselves be) subquestions whose "
    "answers would clearly and strongly influence the answer to a given scope "
    "question. You will be given the scope question and a list of candidate "
    "top-level questions. Return the short IDs of the candidates that are most "
    "promising as places to look for such subquestions. Order the result by "
    "expected relevance, with the most promising first."
)


async def select_seed_questions(
    scope: Page,
    db: DB,
    *,
    limit: int = 10,
) -> list[Page]:
    """Return up to *limit* human-authored questions most relevant to *scope*.

    1. Fetch all human-authored questions in the workspace.
    2. Drop the scope question itself.
    3. If <= limit candidates remain, return them as-is (no LLM call).
    4. Otherwise rank with a single Sonnet call and return the top *limit*.
    """
    settings = get_settings()
    humans = await db.get_human_questions()
    candidates = [q for q in humans if q.id != scope.id]

    if len(candidates) <= limit:
        return candidates

    candidate_lines = [f"{c.id[:8]}: {c.headline}" for c in candidates]
    user_message = (
        "Scope question:\n"
        f"  `{scope.id[:8]}` -- {scope.headline}\n\n"
        f"{scope.content or scope.abstract}\n\n"
        f"Candidate top-level questions ({len(candidates)} total). "
        f"Select up to {limit}, ordered by expected relevance:\n\n"
        + "\n".join(candidate_lines)
    )

    model = (
        settings.model
        if settings.is_test_mode or settings.is_smoke_test
        else settings.sonnet_model
    )

    result = await structured_call(
        _SYSTEM_PROMPT,
        user_message,
        response_model=TopLevelRelevanceRanking,
        model=model,
    )

    if result.parsed is None:
        log.warning(
            "seed selection ranker returned no data; falling back to first %d", limit
        )
        return candidates[:limit]

    ranking = result.parsed
    by_short_id: dict[str, Page] = {c.id[:8]: c for c in candidates}
    selected: list[Page] = []
    seen: set[str] = set()
    for sid in ranking.selected_short_ids:
        sid = sid.strip()
        page = by_short_id.get(sid)
        if page is None or page.id in seen:
            continue
        selected.append(page)
        seen.add(page.id)
        if len(selected) >= limit:
            break
    return selected
