"""
Build context text from workspace pages for injection into LLM prompts.
"""
from typing import Optional

from differential.database import DB
from differential.models import Page, PageLink, PageType, Workspace
from differential.workspace_map import build_workspace_map


def format_page(page: Page, db: Optional[DB] = None) -> str:
    """Format a single page as readable text for LLM context."""
    extra = page.extra or {}
    lines = [
        f"### [{page.page_type.value.upper()}] {page.summary}",
        f"ID: {page.id}",
        f"Epistemic status: {page.epistemic_status:.1f}/5 ({page.epistemic_type})",
    ]

    for k, v in extra.items():
        lines.append(f"{k}: {v}")

    lines += ["", page.content]

    # For questions, include considerations if db provided
    if db and page.page_type == PageType.QUESTION:
        considerations = db.get_considerations_for_question(page.id)
        if considerations:
            lines.append("")
            lines.append("**Considerations:**")
            for claim, link in considerations:
                direction = link.direction.value if link.direction else "neutral"
                lines.append(
                    f"- [{direction}, strength {link.strength:.1f}/5] "
                    f"{claim.summary} (ID: {claim.id})"
                )
                if link.reasoning:
                    lines.append(f"  Reasoning: {link.reasoning}")

        judgements = db.get_judgements_for_question(page.id)
        if judgements:
            lines.append("")
            lines.append("**Existing judgements:**")
            for j in judgements:
                lines.append(f"- {j.summary} (confidence: {j.epistemic_status:.1f}/5)")

    return "\n".join(lines)


def format_pages_block(pages: list[Page], header: str, db: Optional[DB] = None) -> str:
    if not pages:
        return ""
    parts = [f"## {header}", ""]
    for page in pages:
        parts.append(format_page(page, db=db))
        parts.append("")
    return "\n".join(parts)



def build_context_for_question(
    question_id: str,
    db: DB,
    include_considerations: bool = True,
    include_judgements: bool = True,
    workspace: Workspace = Workspace.RESEARCH,
) -> str:
    """Build full context text for working on a question."""
    question = db.get_page(question_id)
    if not question:
        return f"[Question {question_id} not found]"

    parts = ["# Workspace Context", ""]
    parts.append(format_page(question, db=db))
    parts.append("")

    if include_considerations:
        considerations = db.get_considerations_for_question(question_id)
        if considerations:
            parts.append("## Existing Considerations")
            parts.append("")
            for claim, link in considerations:
                direction = link.direction.value if link.direction else "neutral"
                parts.append(
                    f"**[{direction.upper()}, strength {link.strength:.1f}/5]** "
                    f"{claim.summary} (ID: `{claim.id}`)"
                )
                parts.append(claim.content)
                if link.reasoning:
                    parts.append(f"*Link reasoning: {link.reasoning}*")
                parts.append("")

    if include_judgements:
        judgements = db.get_judgements_for_question(question_id)
        if judgements:
            parts.append("## Existing Judgements")
            parts.append("")
            for j in judgements:
                parts.append(format_page(j))
                parts.append("")

    return "\n".join(parts)


def _build_question_index(question_id: str, db: DB, indent: int = 0) -> list[str]:
    """Recursively build a flat index of all questions in the tree with their IDs.
    Includes consideration count, last scout fruit/date, and hypothesis flag."""
    question = db.get_page(question_id)
    if not question:
        return []
    prefix = "  " * indent
    tag = "[scope]" if indent == 0 else "[child]"

    extra = question.extra or {}
    is_hypothesis = extra.get("hypothesis", False)
    hypothesis_tag = " [hypothesis]" if is_hypothesis else ""

    n_cons = len(db.get_considerations_for_question(question_id))
    scout_info = db.get_last_scout_info(question_id)
    if scout_info:
        date_str = scout_info[0][:10]
        fruit = scout_info[1]
        fruit_str = f"fruit={fruit}" if fruit is not None else "fruit=?"
        scout_str = f"{fruit_str} · {date_str}"
    else:
        scout_str = "never scouted"

    lines = [
        f"{prefix}{tag}{hypothesis_tag} `{question_id}` — {question.summary} "
        f"({n_cons} cons · {scout_str})"
    ]
    for child in db.get_child_questions(question_id):
        lines.extend(_build_question_index(child.id, db, indent + 1))
    return lines


def build_call_context(
    question_id: str,
    db: DB,
    extra_page_ids: Optional[list[str]] = None,
) -> tuple[str, dict[str, str]]:
    """Build full context for a scout/assess/ingest call.

    Prepends a compact workspace map, then the detailed working context for
    the given question. Any extra_page_ids (full UUIDs) are appended as
    pre-loaded pages at the end.

    Returns (context_text, short_id_to_full_uuid).
    """
    map_text, short_id_map = build_workspace_map(db)
    working_context = build_context_for_question(question_id, db)

    parts = [
        map_text,
        "---",
        "",
        "## Working Context",
        "",
        working_context,
    ]

    if extra_page_ids:
        for pid in extra_page_ids:
            page = db.get_page(pid)
            if page:
                parts += ["", "---", "", f"## Pre-loaded Page: `{pid[:8]}`", ""]
                parts.append(format_page(page, db=db))

    return "\n".join(parts), short_id_map


def build_prioritization_context(db: DB, scope_question_id: Optional[str] = None) -> str:
    """Build context for a prioritization call."""
    parts = ["# Prioritization Context", ""]

    if scope_question_id:
        question = db.get_page(scope_question_id)
        if question:
            # Index of all dispatchable question IDs — prevents hallucination
            index_lines = _build_question_index(scope_question_id, db)
            parts.append("## Questions available to dispatch on")
            parts.append("")
            parts.append("Use only these exact IDs in your dispatch tags:")
            parts.append("")
            parts.extend(index_lines)
            parts.append("")

            # Full detail on scope question and children
            parts.append("## Scope Question")
            parts.append("")
            parts.append(format_page(question, db=db))
            parts.append("")

            children = db.get_child_questions(scope_question_id)
            if children:
                parts.append("## Sub-questions")
                parts.append("")
                for child in children:
                    parts.append(format_page(child, db=db))
                    parts.append("")

    # Sources and ingest history
    source_pages = db.get_pages(page_type=PageType.SOURCE)
    if source_pages:
        ingest_history = db.get_ingest_history()
        parts.append("## Sources and Ingest History")
        parts.append("")
        for src in source_pages:
            src_extra = src.extra or {}
            filename = src_extra.get("filename", src.id[:8])
            char_count = src_extra.get("char_count", len(src.content))
            question_ids = ingest_history.get(src.id, [])
            parts.append(f"[SRC] `{src.id[:8]}` — {filename} ({char_count:,} chars)")
            if question_ids:
                for qid in question_ids:
                    q = db.get_page(qid)
                    q_summary = q.summary[:60] if q else qid[:8]
                    parts.append(f"  Ingested for: `{qid[:8]}` — {q_summary}")
            else:
                parts.append("  Not yet ingested for any question")
        parts.append("")

    return "\n".join(parts)
