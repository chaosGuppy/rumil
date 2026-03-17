"""
Build a compact, LLM-readable workspace map for context injection.

Returns a map text and a short_id → full_uuid lookup dict.
Short IDs are the first 8 characters of each page UUID.
"""

from rumil.database import DB
from rumil.models import Page, PageType


def _short_id(full_uuid: str) -> str:
    return full_uuid[:8]


async def _build_question_lines(
    question: Page,
    db: DB,
    short_id_map: dict[str, str],
    indent: int = 0,
) -> list[str]:
    prefix = "  " * indent
    sid = _short_id(question.id)
    short_id_map[sid] = question.id

    considerations = await db.get_considerations_for_question(question.id)
    judgements = await db.get_judgements_for_question(question.id)
    children = await db.get_child_questions(question.id)

    n_cons = len(considerations)
    n_j = len(judgements)
    n_sub = len(children)

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

    for j in judgements:
        j_sid = _short_id(j.id)
        short_id_map[j_sid] = j.id
        lines.append(f"{prefix}  [J {j.epistemic_status:.1f}] `{j_sid}` — {j.summary}")

    for child in children:
        lines.extend(await _build_question_lines(child, db, short_id_map, indent + 1))

    return lines


async def build_workspace_map(
    db: DB,
    collapse_depth: int | None = None,
) -> tuple[str, dict[str, str]]:
    """Compact LLM-readable map of the entire workspace.

    Returns (map_text, short_id_to_full_uuid).
    collapse_depth is accepted but currently ignored (reserved for future branch collapsing).
    """
    short_id_map: dict[str, str] = {}
    parts = [
        "## Workspace Map",
        "",
        "Use short IDs with LOAD_PAGE to fetch full content for any page.",
        "",
    ]

    root_questions = await db.get_root_questions()
    if root_questions:
        parts.append("### Questions")
        parts.append("")
        for q in root_questions:
            lines = await _build_question_lines(q, db, short_id_map, indent=0)
            parts.extend(lines)
            parts.append("")

    claim_pages = await db.get_pages(page_type=PageType.CLAIM)
    if claim_pages:
        parts.append("### Claims")
        parts.append("")
        for claim in claim_pages:
            c_sid = _short_id(claim.id)
            short_id_map[c_sid] = claim.id
            parts.append(
                f"[C {claim.epistemic_status:.1f}] `{c_sid}` — {claim.summary}"
            )
        parts.append("")

    source_pages = await db.get_pages(page_type=PageType.SOURCE)
    if source_pages:
        parts.append("### Sources")
        parts.append("")
        for src in source_pages:
            extra = src.extra or {}
            filename = extra.get("filename", src.id[:8])
            char_count = extra.get("char_count", len(src.content))
            s_sid = _short_id(src.id)
            short_id_map[s_sid] = src.id
            parts.append(f"[SRC] `{s_sid}` — {filename} ({char_count:,} chars)")
            if src.summary and src.summary != filename:
                summary_line = src.summary.replace("\n", " ")
                parts.append(f"       {summary_line}")
        parts.append("")

    return "\n".join(parts), short_id_map
