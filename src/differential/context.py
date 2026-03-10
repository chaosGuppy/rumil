"""
Build context text from workspace pages for injection into LLM prompts.
"""

import logging

from differential.database import DB
from differential.models import Page, PageType, Workspace
from differential.workspace_map import build_workspace_map

log = logging.getLogger(__name__)


async def collect_subtree_ids(question_id: str, db: DB) -> set[str]:
    """Recursively collect all question IDs in a subtree (inclusive)."""
    result = {question_id}
    for child in await db.get_child_questions(question_id):
        result |= await collect_subtree_ids(child.id, db)
    return result


async def format_page(page: Page, db: DB | None = None) -> str:
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
        considerations = await db.get_considerations_for_question(page.id)
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

        judgements = await db.get_judgements_for_question(page.id)
        if judgements:
            lines.append("")
            lines.append("**Existing judgements:**")
            for j in judgements:
                lines.append(f"- {j.summary} (confidence: {j.epistemic_status:.1f}/5)")

    return "\n".join(lines)


async def format_pages_block(pages: list[Page], header: str, db: DB | None = None) -> str:
    if not pages:
        return ""
    parts = [f"## {header}", ""]
    for page in pages:
        parts.append(await format_page(page, db=db))
        parts.append("")
    return "\n".join(parts)


async def build_context_for_question(
    question_id: str,
    db: DB,
    include_considerations: bool = True,
    include_judgements: bool = True,
    workspace: Workspace = Workspace.RESEARCH,
) -> tuple[str, list[str]]:
    """Build full context text for working on a question.

    Returns (context_text, loaded_page_ids) where loaded_page_ids lists every
    page whose full content was included.
    """
    question = await db.get_page(question_id)
    if not question:
        return f"[Question {question_id} not found]", []

    loaded_ids = [question_id]
    parts = ["# Workspace Context", ""]
    parts.append(await format_page(question, db=db))
    parts.append("")

    if include_considerations:
        considerations = await db.get_considerations_for_question(question_id)
        if considerations:
            parts.append("## Existing Considerations")
            parts.append("")
            for claim, link in considerations:
                loaded_ids.append(claim.id)
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
        judgements = await db.get_judgements_for_question(question_id)
        if judgements:
            parts.append("## Existing Judgements")
            parts.append("")
            for j in judgements:
                loaded_ids.append(j.id)
                parts.append(await format_page(j))
                parts.append("")

        children = await db.get_child_questions(question_id)
        child_judgements = []
        for child in children:
            for j in await db.get_judgements_for_question(child.id):
                child_judgements.append((child, j))
        if child_judgements:
            parts.append("## Sub-question Judgements")
            parts.append("")
            for child, j in child_judgements:
                loaded_ids.append(j.id)
                parts.append(f"*On sub-question: {child.summary} (`{child.id}`)*")
                parts.append(await format_page(j))
                parts.append("")

    return "\n".join(parts), loaded_ids


async def _build_question_index(question_id: str, db: DB, indent: int = 0) -> list[str]:
    """Recursively build a flat index of all questions in the tree with their IDs.
    Includes consideration count, last scout fruit/date, and hypothesis flag."""
    question = await db.get_page(question_id)
    if not question:
        return []
    prefix = "  " * indent
    tag = "[scope]" if indent == 0 else "[child]"

    extra = question.extra or {}
    is_hypothesis = extra.get("hypothesis", False)
    hypothesis_tag = " [hypothesis]" if is_hypothesis else ""

    n_cons = len(await db.get_considerations_for_question(question_id))
    scout_info = await db.get_last_scout_info(question_id)
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
    for child in await db.get_child_questions(question_id):
        lines.extend(await _build_question_index(child.id, db, indent + 1))
    return lines


async def build_call_context(
    question_id: str,
    db: DB,
    extra_page_ids: list[str] | None = None,
) -> tuple[str, dict[str, str], list[str]]:
    """Build full context for a scout/assess/ingest call.

    Prepends a compact workspace map, then the detailed working context for
    the given question. Any extra_page_ids (full UUIDs) are appended as
    pre-loaded pages at the end.

    Returns (context_text, short_id_to_full_uuid, working_context_page_ids).
    working_context_page_ids lists pages whose full content was included as
    part of the question's working context (question itself, considerations,
    judgements).
    """
    log.debug("build_call_context: question=%s", question_id[:8])
    map_text, short_id_map = await build_workspace_map(db)
    working_context, working_page_ids = await build_context_for_question(question_id, db)

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
            page = await db.get_page(pid)
            if page:
                parts += ["", "---", "", f"## Pre-loaded Page: `{pid[:8]}`", ""]
                parts.append(await format_page(page, db=db))

    context_text = "\n".join(parts)
    log.debug(
        "build_call_context complete: %d chars, %d working pages, %d extra pages",
        len(context_text), len(working_page_ids), len(extra_page_ids or []),
    )
    return context_text, short_id_map, working_page_ids


async def build_prioritization_context(
    db: DB, scope_question_id: str | None = None
) -> tuple[str, dict[str, str]]:
    """Build context for a prioritization call.

    Returns (context_text, short_id_map) where short_id_map maps 8-char
    short IDs to full UUIDs.
    """
    map_text, short_id_map = await build_workspace_map(db)
    parts = [map_text, "", "---", "", "# Prioritization Context", ""]

    if scope_question_id:
        question = await db.get_page(scope_question_id)
        if question:
            index_lines = await _build_question_index(scope_question_id, db)
            parts.append("## Scope Subtree — Dispatchable Questions")
            parts.append("")
            parts.append(
                "You can only dispatch research calls on questions in this subtree "
                "(or on new subquestions you create during this call). "
                "Use only these exact IDs in your dispatch tags:"
            )
            parts.append("")
            parts.extend(index_lines)
            parts.append("")

            # Full detail on scope question and children
            parts.append("## Scope Question")
            parts.append("")
            parts.append(await format_page(question, db=db))
            parts.append("")

            children = await db.get_child_questions(scope_question_id)
            if children:
                parts.append("## Sub-questions")
                parts.append("")
                for child in children:
                    parts.append(await format_page(child, db=db))
                    parts.append("")

    # Sources and ingest history
    source_pages = await db.get_pages(page_type=PageType.SOURCE)
    if source_pages:
        ingest_history = await db.get_ingest_history()
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
                    q = await db.get_page(qid)
                    q_summary = q.summary[:60] if q else qid[:8]
                    parts.append(f"  Ingested for: `{qid[:8]}` — {q_summary}")
            else:
                parts.append("  Not yet ingested for any question")
        parts.append("")

    return "\n".join(parts), short_id_map
