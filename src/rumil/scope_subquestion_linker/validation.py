"""Proposal validation for the subquestion linker."""

import logging

from rumil.database import DB
from rumil.models import Page, PageType
from rumil.scope_subquestion_linker.tool import LinkerResult

log = logging.getLogger(__name__)


async def validate_proposals(
    result: LinkerResult,
    db: DB,
    scope_id: str,
    current_children_ids: set[str],
) -> list[Page]:
    """Apply semantic validation to a schema-validated LinkerResult.

    Drops ids that point at the scope itself, are already children, are not
    questions, or are unknown. Returns deduped Pages in submission order.
    Issues at most a constant number of DB round trips regardless of how
    many ids the agent submits.
    """
    cleaned_ids = [raw.strip() for raw in result.question_ids if raw.strip()]
    if not cleaned_ids:
        return []

    resolved_map = await db.resolve_page_ids(cleaned_ids)

    ordered_resolved: list[str] = []
    seen: set[str] = set()
    for raw in cleaned_ids:
        resolved = resolved_map.get(raw)
        if resolved is None:
            log.info("dropping %s: not found", raw)
            continue
        if resolved == scope_id:
            log.info("dropping %s: is scope itself", raw)
            continue
        if resolved in current_children_ids:
            log.info("dropping %s: already a child of scope", raw)
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        ordered_resolved.append(resolved)

    if not ordered_resolved:
        return []

    pages_by_id = await db.get_pages_by_ids(ordered_resolved)
    proposed: list[Page] = []
    for resolved in ordered_resolved:
        page = pages_by_id.get(resolved)
        if page is None or page.page_type != PageType.QUESTION:
            log.info("dropping %s: not a question", resolved)
            continue
        proposed.append(page)
    return proposed
