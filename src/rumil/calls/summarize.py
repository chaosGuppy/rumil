"""Summarize call: produce a hierarchical summary of a question subtree."""

import logging
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from rumil.database import DB
from rumil.llm import LLMExchangeMetadata, build_user_message, structured_call
from rumil.models import (
    Call,
    CallStatus,
    CallType,
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)
from rumil.moves.base import write_page_file
from rumil.tracing.trace_events import ContextBuiltEvent
from rumil.tracing.tracer import CallTrace

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are a precise research synthesiser. You will be given the full content of a research "
    "question and the pages associated with it — considerations, judgements, child questions, "
    "and their summaries. Your job is to produce a hierarchical summary of what is known about "
    "this question and its sub-questions. Write for LLM instances that will use this summary "
    "as context: prioritise accuracy, epistemic precision, and information density. Preserve "
    "confidence levels, key qualifications, priority orderings, and causal mechanisms. "
    "Do not pad. Do not add caveats beyond those in the source material."
)

TASK = (
    "Produce a summary of this question subtree with three components:\n\n"
    "CONTENT (~1000 words, shorter is fine if the material doesn't require it): "
    "A structured synthesis covering: the question, the current state of evidence "
    "(key considerations for and against, with their epistemic weight), any judgements "
    "rendered and their confidence levels, what the child questions contribute and their "
    "current state, and what remains uncertain or unresolved.\n\n"
    "SUMMARY_SHORT (~30 words): Fully self-contained. State the question, the current "
    "best answer or stance, and the main caveat. Must make sense with zero prior context.\n\n"
    "SUMMARY_MEDIUM (~200 words): Fully self-contained. Include the core conclusion, "
    "key supporting and opposing considerations, the status of child questions, and "
    "critical uncertainties. Preserve epistemic qualifications.\n\n"
    "Format your response exactly as:\n"
    "CONTENT: <text>\n\n"
    "SUMMARY_SHORT: <text>\n\n"
    "SUMMARY_MEDIUM: <text>"
)


class SummaryOutput(BaseModel):
    content: str = Field(description="Main summary body (~1000 words)")
    summary_short: str = Field(description="Self-contained ~30-word summary")
    summary_medium: str = Field(description="Self-contained ~200-word summary")
    headline: str = Field(
        description=(
            "10-15 word headline for this summary page itself "
            "(not the question — e.g. 'Summary of evidence on X as of [date]')"
        )
    )


def _section(title: str, body: str) -> str:
    return f"### {title}\n\n{body}\n"


async def _build_summary_context(question_id: str, db: DB) -> str:
    question = await db.get_page(question_id)
    if not question:
        raise ValueError(f"Question {question_id} not found")

    parts: list[str] = []

    parts.append(_section("Question (full)", question.content or question.summary))

    considerations = await db.get_considerations_for_question(question_id)
    judgements = await db.get_judgements_for_question(question_id)
    children = await db.get_child_questions(question_id)

    if considerations:
        index_lines = ["Index of direct pages:"]
        for page, link in considerations:
            direction = f" [{link.direction.value}]" if link.direction else ""
            index_lines.append(f"- [consideration{direction}] {page.summary_short or page.summary}")
        for j in judgements:
            index_lines.append(f"- [judgement {j.epistemic_status:.1f}] {j.summary_short or j.summary}")
        for child in children:
            index_lines.append(f"- [child question] {child.summary}")
        parts.append(_section("Index", "\n".join(index_lines)))

    if considerations:
        cons_parts = []
        for page, link in considerations:
            direction = f" [{link.direction.value}]" if link.direction else ""
            cons_parts.append(
                f"**Consideration{direction}** (strength {link.strength:.1f}): "
                f"{page.summary}\n\n{page.content}"
            )
        parts.append(_section("Direct Considerations (full)", "\n\n---\n\n".join(cons_parts)))

    if judgements:
        most_recent = max(judgements, key=lambda j: j.created_at)
        parts.append(_section(
            "Most Recent Judgement (full)",
            f"Epistemic status: {most_recent.epistemic_status:.1f} — {most_recent.epistemic_type}\n\n"
            f"{most_recent.content}",
        ))
        older = [j for j in judgements if j.id != most_recent.id]
        if older:
            older_lines = [f"- [{j.epistemic_status:.1f}] {j.summary}" for j in older]
            parts.append(_section("Earlier Judgements (summaries)", "\n".join(older_lines)))

    if children:
        child_parts = []
        for child in children:
            child_section_parts: list[str] = [f"**Child question:** {child.summary}"]

            child_summary = await db.get_latest_summary_for_question(child.id)
            if child_summary:
                child_section_parts.append(
                    f"**Summary (full):**\n{child_summary.content}"
                )
            else:
                child_section_parts.append("_(No summary available yet)_")

            child_judgements = await db.get_judgements_for_question(child.id)
            if child_judgements:
                most_recent_j = max(child_judgements, key=lambda j: j.created_at)
                medium = most_recent_j.summary_medium or most_recent_j.summary
                child_section_parts.append(f"**Judgement (medium):** {medium}")

            child_considerations = await db.get_considerations_for_question(child.id)
            if child_considerations:
                con_lines = [
                    f"  - {p.summary_short or p.summary}"
                    for p, _ in child_considerations
                ]
                child_section_parts.append(
                    "**Considerations (short):**\n" + "\n".join(con_lines)
                )

            grandchildren = await db.get_child_questions(child.id)
            if grandchildren:
                gc_lines = []
                for gc in grandchildren:
                    gc_summary = await db.get_latest_summary_for_question(gc.id)
                    gc_medium = gc_summary.summary_medium if gc_summary else None
                    gc_judgements = await db.get_judgements_for_question(gc.id)
                    gc_short_j = (
                        max(gc_judgements, key=lambda j: j.created_at).summary_short
                        if gc_judgements else None
                    )
                    gc_lines.append(
                        f"  - **{gc.summary}**"
                        + (f"\n    Summary: {gc_medium}" if gc_medium else "")
                        + (f"\n    Judgement: {gc_short_j}" if gc_short_j else "")
                    )
                child_section_parts.append(
                    "**Grandchild questions:**\n" + "\n".join(gc_lines)
                )

            child_parts.append("\n\n".join(child_section_parts))

        parts.append(_section("Child Questions", "\n\n---\n\n".join(child_parts)))

    return "\n\n".join(parts)


async def _supersede_old_summaries(question_id: str, new_summary_id: str, db: DB) -> None:
    """Mark any existing summary pages for this question as superseded."""
    links = await db.get_links_to(question_id)
    for link in links:
        if link.link_type != LinkType.SUMMARIZES:
            continue
        old = await db.get_page(link.from_page_id)
        if old and old.is_active() and old.page_type == PageType.SUMMARY and old.id != new_summary_id:
            await db.client.table("pages").update(
                {"is_superseded": True, "superseded_by": new_summary_id}
            ).eq("id", old.id).execute()
            log.info("Superseded old summary %s with %s", old.id[:8], new_summary_id[:8])


async def summarize_question(
    question_id: str,
    db: DB,
    parent_call_id: str | None = None,
) -> str | None:
    """Generate a summary page for a question subtree. Free (not budget-counted).

    Returns the new summary page ID, or None on failure.
    """
    log.info("summarize_question: question=%s", question_id[:8])

    call = await db.create_call(
        CallType.SUMMARIZE,
        scope_page_id=question_id,
        parent_call_id=parent_call_id,
    )
    trace = CallTrace(call.id, db)

    try:
        context = await _build_summary_context(question_id, db)
        await trace.record(ContextBuiltEvent())
        user_message = build_user_message(context, TASK)

        meta = LLMExchangeMetadata(call_id=call.id, phase="summarize", trace=trace, db=db)
        result = await structured_call(
            system_prompt=SYSTEM_PROMPT,
            user_message=user_message,
            response_model=SummaryOutput,
            max_tokens=4096,
            metadata=meta,
            db=db,
        )

        if not result.data:
            log.warning("summarize_question: structured call returned no data for %s", question_id[:8])
            await _fail_call(call, db)
            return None

        data = result.data
        question = await db.get_page(question_id)
        headline = data.get("headline") or f"Summary of {question.summary[:60] if question else question_id[:8]}"

        page = Page(
            page_type=PageType.SUMMARY,
            layer=PageLayer.SQUIDGY,
            workspace=Workspace.RESEARCH,
            content=data.get("content", ""),
            summary=headline,
            summary_short=data.get("summary_short", ""),
            summary_medium=data.get("summary_medium", ""),
            epistemic_status=0.0,
            epistemic_type="derived",
            provenance_model="claude-sonnet-4-6",
            provenance_call_type=CallType.SUMMARIZE.value,
            provenance_call_id=call.id,
        )
        await db.save_page(page)
        write_page_file(page)

        link = PageLink(
            from_page_id=page.id,
            to_page_id=question_id,
            link_type=LinkType.SUMMARIZES,
            reasoning="Auto-generated subtree summary",
        )
        await db.save_link(link)

        await _supersede_old_summaries(question_id, page.id, db)

        call.status = CallStatus.COMPLETE
        call.completed_at = datetime.now(UTC)
        call.result_summary = f"Summary created: {headline[:80]}"
        await db.save_call(call)

        log.info("summarize_question complete: page=%s", page.id[:8])
        return page.id

    except Exception as e:
        log.error("summarize_question failed for %s: %s", question_id[:8], e, exc_info=True)
        await _fail_call(call, db)
        return None


async def _fail_call(call: Call, db: DB) -> None:
    call.status = CallStatus.FAILED
    call.completed_at = datetime.now(UTC)
    await db.save_call(call)
