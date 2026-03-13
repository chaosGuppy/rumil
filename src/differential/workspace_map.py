"""
Build a compact, LLM-readable workspace map for context injection.

Returns a map text and a short_id → full_uuid lookup dict.
Short IDs are the first 8 characters of each page UUID.
"""

from datetime import datetime

from differential.database import DB
from differential.models import Page, PageType


def _short_id(full_uuid: str) -> str:
    return full_uuid[:8]


async def _build_question_lines(
    question: Page,
    db: DB,
    short_id_map: dict[str, str],
    indent: int = 0,
    collapse_depth: int | None = None,
    created_after: datetime | None = None,
) -> list[str]:
    prefix = "  " * indent
    sid = _short_id(question.id)

    considerations = await db.get_considerations_for_question(question.id)
    judgements = await db.get_judgements_for_question(question.id)
    children = await db.get_child_questions(question.id)

    if created_after:
        considerations = [
            (c, l) for c, l in considerations if c.created_at > created_after
        ]
        judgements = [j for j in judgements if j.created_at > created_after]

    # Build child lines (recursive, respects created_after)
    child_lines: list[str] = []
    children_shown = 0
    for child in children:
        cl = await _build_question_lines(
            child, db, short_id_map, indent + 1, collapse_depth, created_after
        )
        if cl:
            child_lines.extend(cl)
            children_shown += 1

    if created_after:
        question_is_new = question.created_at > created_after
        has_content = considerations or judgements or child_lines or question_is_new
        if not has_content:
            return []

    short_id_map[sid] = question.id

    n_cons = len(considerations)
    n_j = len(judgements)
    n_sub = children_shown if created_after else len(children)

    stats_parts = []
    if n_cons:
        stats_parts.append(f"{n_cons} con{'s' if n_cons != 1 else ''}")
    if n_j:
        stats_parts.append(f"{n_j} judgement{'s' if n_j != 1 else ''}")
    if n_sub:
        stats_parts.append(f"{n_sub} sub-Q{'s' if n_sub != 1 else ''}")
    stats = " · ".join(stats_parts) if stats_parts else "empty"

    extra = question.extra or {}
    hypothesis_tag = " [hypothesis]" if extra.get("hypothesis") else ""
    lines = [f"{prefix}[Q]{hypothesis_tag} `{sid}` — {question.summary} ({stats})"]

    # Considerations
    for claim, link in considerations:
        c_sid = _short_id(claim.id)
        short_id_map[c_sid] = claim.id
        lines.append(
            f"{prefix}  [{link.strength:.1f}] `{c_sid}` — {claim.summary}"
        )

    # Judgements
    for j in judgements:
        j_sid = _short_id(j.id)
        short_id_map[j_sid] = j.id
        lines.append(f"{prefix}  [J {j.epistemic_status:.1f}] `{j_sid}` — {j.summary}")

    # Sub-questions (recursive, already built above)
    lines.extend(child_lines)

    return lines


async def build_workspace_map(
    db: DB,
    collapse_depth: int | None = None,
    created_after: datetime | None = None,
) -> tuple[str, dict[str, str]]:
    """Compact LLM-readable map of the entire workspace.

    Returns (map_text, short_id_to_full_uuid).
    collapse_depth is accepted but currently ignored (reserved for future branch collapsing).
    When created_after is set, only pages created after that time are included.
    Questions are shown as context headers when they have new sub-items.
    """
    short_id_map: dict[str, str] = {}
    parts = [
        "## Workspace Map",
        "",
        "Use short IDs with LOAD_PAGE to fetch full content for any page.",
        "",
    ]

    root_questions = await db.get_root_questions()
    question_lines: list[str] = []
    for q in root_questions:
        lines = await _build_question_lines(
            q, db, short_id_map, indent=0, collapse_depth=collapse_depth,
            created_after=created_after,
        )
        if lines:
            question_lines.extend(lines)
            question_lines.append("")

    if question_lines:
        parts.append("### Questions")
        parts.append("")
        parts.extend(question_lines)

    # Sources section — all source pages regardless of workspace
    source_pages = await db.get_pages(page_type=PageType.SOURCE)
    if created_after:
        source_pages = [s for s in source_pages if s.created_at > created_after]
    source_lines: list[str] = []
    for src in source_pages:
        extra = src.extra or {}
        filename = extra.get("filename", src.id[:8])
        char_count = extra.get("char_count", len(src.content))
        s_sid = _short_id(src.id)
        short_id_map[s_sid] = src.id
        source_lines.append(f"[SRC] `{s_sid}` — {filename} ({char_count:,} chars)")
        if src.summary and src.summary != filename:
            summary_line = src.summary.replace("\n", " ")
            source_lines.append(f"       {summary_line}")

    if source_lines:
        parts.append("### Sources")
        parts.append("")
        parts.extend(source_lines)
        parts.append("")

    return "\n".join(parts), short_id_map
