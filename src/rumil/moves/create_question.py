"""CREATE_QUESTION move: create a research question."""

import logging
from collections.abc import Sequence

from pydantic import Field

from rumil.database import DB
from rumil.embeddings import embed_query, search_pages_by_vector
from rumil.models import (
    Call,
    LinkType,
    MoveType,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)
from rumil.moves.base import CreatePagePayload, MoveDef, MoveResult, create_page
from rumil.moves.link_child_question import ChildQuestionLinkFields
from rumil.question_triage import auto_triage_and_save
from rumil.settings import get_settings
from rumil.task_shape import auto_tag_and_save

log = logging.getLogger(__name__)


async def _find_duplicate_question(
    headline: str,
    db: DB,
    threshold: float,
) -> tuple[str, float] | None:
    """Embed *headline* and return (page_id, similarity) if a QUESTION in the
    same project exceeds *threshold*, else None. Returns None and logs a
    warning if embedding fails — callers should fall back to creating the page.
    """
    try:
        embedding = await embed_query(headline)
    except Exception:
        log.warning(
            "Subquestion dedup embed_query failed for %r; creating page anyway",
            headline[:80],
            exc_info=True,
        )
        return None

    try:
        results = await search_pages_by_vector(
            db,
            embedding,
            match_threshold=threshold,
            match_count=5,
            workspace=Workspace.RESEARCH,
        )
    except Exception:
        log.warning(
            "Subquestion dedup search_pages_by_vector failed for %r; creating page anyway",
            headline[:80],
            exc_info=True,
        )
        return None

    for page, similarity in results:
        if page.page_type != PageType.QUESTION:
            continue
        if similarity >= threshold:
            return page.id, similarity
    return None


class CreateQuestionPayload(CreatePagePayload):
    links: list[ChildQuestionLinkFields] = Field(
        default_factory=list,
        description=(
            "Parent question links to create for this question. Each entry "
            "links an existing question as the parent of this new question."
        ),
    )


async def _link_child_questions(
    target_id: str,
    links: Sequence[ChildQuestionLinkFields],
    db: DB,
) -> str | None:
    """Attach each parent link in *links* as a CHILD_QUESTION edge to *target_id*.

    Returns the id of the first resolved parent (used downstream for triage
    parent linkage), or None if no parents resolved.
    """
    first_parent_id: str | None = None
    for link_spec in links:
        resolved = await db.resolve_page_id(link_spec.parent_id)
        if not resolved:
            log.warning(
                "Inline child question link skipped: parent %s not found",
                link_spec.parent_id,
            )
            continue

        if first_parent_id is None:
            first_parent_id = resolved

        await db.save_link(
            PageLink(
                from_page_id=resolved,
                to_page_id=target_id,
                link_type=LinkType.CHILD_QUESTION,
                reasoning=link_spec.reasoning,
                role=link_spec.role,
                impact_on_parent_question=link_spec.impact_on_parent_question,
            )
        )
        log.info(
            "Inline child question linked: %s -> %s",
            resolved[:8],
            target_id[:8],
        )
    return first_parent_id


def _dedup_message(existing_id: str, similarity: float, headline: str) -> str:
    return (
        f"Near-duplicate of existing question [{existing_id[:8]}] "
        f"(cosine similarity {similarity:.3f}): {headline}. "
        "Reused the existing page; no new page created."
    )


async def execute(payload: CreateQuestionPayload, call: Call, db: DB) -> MoveResult:
    threshold = get_settings().subquestion_dedup_similarity_threshold
    dedup = await _find_duplicate_question(payload.headline, db, threshold)
    if dedup is not None:
        existing_id, similarity = dedup
        log.info(
            "Subquestion dedup: headline=%r matched existing question %s (similarity %.3f)",
            payload.headline[:80],
            existing_id[:8],
            similarity,
        )
        await _link_child_questions(existing_id, payload.links, db)
        return MoveResult(
            message=_dedup_message(existing_id, similarity, payload.headline),
            trace_extra={
                "deduped": True,
                "existing_page_id": existing_id,
                "similarity": similarity,
                "candidate_headline": payload.headline,
            },
        )

    result = await create_page(payload, call, db, PageType.QUESTION, PageLayer.SQUIDGY)
    if not result.created_page_id:
        return result

    await auto_tag_and_save(result.created_page_id, payload.headline, payload.content, db)

    first_parent_id = await _link_child_questions(result.created_page_id, payload.links, db)

    await auto_triage_and_save(db, result.created_page_id, parent_id=first_parent_id)
    return result


async def execute_scout_question(
    payload: CreatePagePayload,
    call: Call,
    db: DB,
) -> MoveResult:
    """Create a question and auto-link it as a child of the call's scope question."""
    threshold = get_settings().subquestion_dedup_similarity_threshold
    dedup = await _find_duplicate_question(payload.headline, db, threshold)
    if dedup is not None:
        existing_id, similarity = dedup
        log.info(
            "Scout subquestion dedup: headline=%r matched existing question %s (similarity %.3f)",
            payload.headline[:80],
            existing_id[:8],
            similarity,
        )
        if call.scope_page_id and call.scope_page_id != existing_id:
            await db.save_link(
                PageLink(
                    from_page_id=call.scope_page_id,
                    to_page_id=existing_id,
                    link_type=LinkType.CHILD_QUESTION,
                    reasoning="Auto-linked to scope question (dedup)",
                )
            )
        return MoveResult(
            message=_dedup_message(existing_id, similarity, payload.headline),
            trace_extra={
                "deduped": True,
                "existing_page_id": existing_id,
                "similarity": similarity,
                "candidate_headline": payload.headline,
            },
        )

    result = await create_page(payload, call, db, PageType.QUESTION, PageLayer.SQUIDGY)
    if not result.created_page_id:
        return result

    await auto_tag_and_save(result.created_page_id, payload.headline, payload.content, db)

    if call.scope_page_id:
        await db.save_link(
            PageLink(
                from_page_id=call.scope_page_id,
                to_page_id=result.created_page_id,
                link_type=LinkType.CHILD_QUESTION,
                reasoning="Auto-linked to scope question",
            )
        )
        log.info(
            "Scout question auto-linked: %s -> %s",
            call.scope_page_id[:8],
            result.created_page_id[:8],
        )

    await auto_triage_and_save(db, result.created_page_id, parent_id=call.scope_page_id)
    return result


SCOUT_MOVE = MoveDef(
    move_type=MoveType.CREATE_SCOUT_QUESTION,
    name="create_question",
    description=(
        "Create a new research question — an open problem for investigation. "
        "The question is automatically linked as a child of the scope question."
    ),
    schema=CreatePagePayload,
    execute=execute_scout_question,
)


MOVE = MoveDef(
    move_type=MoveType.CREATE_QUESTION,
    name="create_question",
    description=(
        "Create a new research question — an open problem for investigation. "
        "Use the links field to simultaneously attach this question as a "
        "child of one or more parent questions."
    ),
    schema=CreateQuestionPayload,
    execute=execute,
)
