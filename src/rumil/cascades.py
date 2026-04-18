"""Cascade detection: flag dependent pages for review when scores shift significantly."""

import logging
from collections.abc import Sequence

from rumil.database import DB
from rumil.models import Suggestion, SuggestionType
from rumil.settings import get_settings

log = logging.getLogger(__name__)

CASCADE_FIELDS = {"credence", "robustness", "importance"}


def _threshold_for(field: str) -> int:
    """Per-field threshold, pulled from settings so ops can tune these live."""
    settings = get_settings()
    if field == "credence":
        return settings.cascade_credence_delta_threshold
    if field == "robustness":
        return settings.cascade_robustness_delta_threshold
    if field == "importance":
        return settings.cascade_importance_delta_threshold
    return 2


def _significant_changes(
    changes: dict[str, tuple[object, object]],
) -> dict[str, tuple[object, object]]:
    """Filter to changes that cross the per-field cascade threshold."""
    significant: dict[str, tuple[object, object]] = {}
    for field, (old, new) in changes.items():
        if field not in CASCADE_FIELDS:
            continue
        if not isinstance(old, int) or not isinstance(new, int):
            continue
        if abs(new - old) >= _threshold_for(field):
            significant[field] = (old, new)
    return significant


def _describe_changes(
    changes: dict[str, tuple[object, object]],
) -> str:
    parts: list[str] = []
    for field, (old, new) in changes.items():
        parts.append(f"{field}: {old} -> {new}")
    return "; ".join(parts)


async def check_cascades(
    db: DB,
    changed_page_id: str,
    changes: dict[str, tuple[object, object]],
    *,
    call_id: str = "",
) -> Sequence[Suggestion]:
    """Check for cascade-worthy changes and create suggestions.

    A change is cascade-worthy when credence or robustness shifts by 2+ points,
    or importance shifts by 2+ levels.
    """
    significant = _significant_changes(changes)
    if not significant:
        return []

    changed_page = await db.get_page(changed_page_id)
    if not changed_page:
        log.warning("Cascade check: source page %s not found", changed_page_id[:8])
        return []

    dependents = await db.get_dependents(changed_page_id)
    if not dependents:
        return []

    description = _describe_changes(significant)
    log.info(
        "Cascade detected: page %s changed (%s), %d dependents",
        changed_page_id[:8],
        description,
        len(dependents),
    )

    suggestions: list[Suggestion] = []
    for dep_page, link in dependents:
        payload = {
            "changed_page_id": changed_page_id,
            "changed_headline": changed_page.headline,
            "changes": {k: {"old": v[0], "new": v[1]} for k, v in significant.items()},
            "dependent_page_id": dep_page.id,
            "dependent_headline": dep_page.headline,
            "reasoning": (
                f"Page [{changed_page_id[:8]}] "
                f'"{changed_page.headline}" changed significantly ({description}). '
                f"Page [{dep_page.id[:8]}] "
                f'"{dep_page.headline}" depends on it and may need re-evaluation.'
            ),
        }
        suggestion = Suggestion(
            project_id=db.project_id,
            workspace=changed_page.workspace.value,
            run_id=db.run_id,
            suggestion_type=SuggestionType.CASCADE_REVIEW,
            target_page_id=dep_page.id,
            source_page_id=changed_page_id,
            payload=payload,
            staged=db.staged,
        )
        await db.save_suggestion(suggestion)
        suggestions.append(suggestion)

    log.info("Created %d cascade_review suggestions", len(suggestions))
    return suggestions
