"""Summarize call: produce a hierarchical summary of a question subtree."""

import logging
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from rumil.context import format_page
from rumil.database import DB
from rumil.llm import LLMExchangeMetadata, build_user_message, structured_call
from rumil.models import (
    Call,
    CallStatus,
    CallType,
    LinkType,
    Page,
    PageDetail,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)
from rumil.prompts import PROMPTS_DIR as _PROMPTS_DIR
from rumil.settings import get_settings
from rumil.tracing.page_load_tracking import page_track_scope
from rumil.tracing.trace_events import ContextBuiltEvent, ErrorEvent, PageRef
from rumil.tracing.tracer import CallTrace, set_trace

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (_PROMPTS_DIR / "summarize.md").read_text()

TASK = (
    "Produce a summary of this question subtree with three components:\n\n"
    "CONTENT (~1000 words, shorter is fine if the material doesn't require it): "
    "A structured synthesis covering: the question, the current state of evidence "
    "(key considerations for and against, with their epistemic weight), any judgements "
    "rendered and their confidence levels, what the child questions contribute and their "
    "current state, and what remains uncertain or unresolved.\n\n"
    "HEADLINE (~30 words): Fully self-contained. State the question, the current "
    "best answer or stance, and the main caveat. Must make sense with zero prior context.\n\n"
    "ABSTRACT (~200 words): Fully self-contained. Include the core conclusion, "
    "key supporting and opposing considerations, the status of child questions, and "
    "critical uncertainties. Preserve epistemic qualifications."
)


class SummaryOutput(BaseModel):
    content: str = Field(description="Main summary body (~1000 words)")
    headline: str = Field(
        description=(
            "Self-contained summary of ~30 words. State the core topic and conclusion "
            "so a reader with no prior context understands what the page is about and "
            "what it concludes. Include the key finding and main caveat if space allows. "
            "Must stand alone — never use language that only makes sense relative to a "
            "particular question or investigation."
        )
    )
    abstract: str = Field(
        description=(
            "Self-contained summary of ~200 words. Include: the core conclusion, "
            "the main supporting reasoning or evidence, key counter-arguments and why "
            "they were discounted, and the critical uncertainties or dependencies. "
            "Preserve epistemic qualifications, confidence levels, and priority orderings. "
            "Must make sense with zero prior context."
        )
    )
    page_headline: str = Field(
        description=(
            "10-15 word headline for this summary page itself "
            "(not the question — e.g. 'Summary of evidence on X as of [date]')"
        )
    )


def _section(title: str, body: str) -> str:
    return f"### {title}\n\n{body}\n"


async def _build_summary_context(question_id: str, db: DB) -> tuple[str, list[PageRef]]:
    question = await db.get_page(question_id)
    if not question:
        raise ValueError(f"Question {question_id} not found")

    parts: list[str] = []

    def ref(page: Page) -> PageRef:
        return PageRef(id=page.id, headline=page.headline)

    page_refs: list[PageRef] = [ref(question)]

    summary_tag = {"source": "summary_context"}

    parts.append(
        _section(
            "Question (full)",
            await format_page(
                question,
                PageDetail.CONTENT,
                linked_detail=None,
                db=db,
                track=True,
                track_tags=summary_tag,
            ),
        )
    )

    considerations = await db.get_considerations_for_question(question_id)
    judgements = await db.get_judgements_for_question(question_id)
    children = await db.get_child_questions(question_id)
    page_refs.extend(ref(p) for p, _ in considerations)
    page_refs.extend(ref(j) for j in judgements)
    page_refs.extend(ref(c) for c in children)

    if considerations:
        index_lines = ["Index of direct pages:"]
        for page, link in considerations:
            direction = f" [{link.direction.value}]" if link.direction else ""
            index_lines.append(f"- [consideration{direction}] {page.headline}")
        for j in judgements:
            index_lines.append(f"- [judgement R{j.robustness}] {j.headline}")
        for child in children:
            index_lines.append(f"- [child question] {child.headline}")
        parts.append(_section("Index", "\n".join(index_lines)))

    if considerations:
        cons_parts = []
        for page, link in considerations:
            direction = f" [{link.direction.value}]" if link.direction else ""
            formatted = await format_page(
                page,
                PageDetail.CONTENT,
                linked_detail=None,
                db=db,
                track=True,
                track_tags=summary_tag,
            )
            cons_parts.append(f"**Consideration{direction}:**\n\n{formatted}")
        parts.append(_section("Direct Considerations (full)", "\n\n---\n\n".join(cons_parts)))

    if judgements:
        most_recent = max(judgements, key=lambda j: j.created_at)
        formatted_j = await format_page(
            most_recent,
            PageDetail.CONTENT,
            linked_detail=None,
            db=db,
            track=True,
            track_tags=summary_tag,
        )
        parts.append(_section("Most Recent Judgement (full)", formatted_j))
        older = [j for j in judgements if j.id != most_recent.id]
        if older:
            older_lines = [
                await format_page(j, PageDetail.HEADLINE, track=True, track_tags=summary_tag)
                for j in older
            ]
            parts.append(
                _section(
                    "Earlier Judgements (summaries)",
                    "\n".join(f"- {line}" for line in older_lines),
                )
            )

    if children:
        child_parts = []
        for child in children:
            child_section_parts: list[str] = [f"**Child question:** {child.headline}"]

            child_summary = await db.get_latest_summary_for_question(child.id)
            if child_summary:
                page_refs.append(ref(child_summary))
                formatted_cs = await format_page(
                    child_summary,
                    PageDetail.CONTENT,
                    linked_detail=None,
                    db=db,
                    track=True,
                    track_tags=summary_tag,
                )
                child_section_parts.append(f"**Summary (full):**\n{formatted_cs}")
            else:
                child_section_parts.append("_(No summary available yet)_")

            child_judgements = await db.get_judgements_for_question(child.id)
            if child_judgements:
                page_refs.extend(ref(j) for j in child_judgements)
                most_recent_j = max(child_judgements, key=lambda j: j.created_at)
                formatted_cj = await format_page(
                    most_recent_j,
                    PageDetail.ABSTRACT,
                    linked_detail=None,
                    db=db,
                    track=True,
                    track_tags=summary_tag,
                )
                child_section_parts.append(f"**Judgement (medium):**\n{formatted_cj}")

            child_considerations = await db.get_considerations_for_question(child.id)
            if child_considerations:
                page_refs.extend(ref(p) for p, _ in child_considerations)
                con_lines = [
                    "  - "
                    + await format_page(p, PageDetail.HEADLINE, track=True, track_tags=summary_tag)
                    for p, _ in child_considerations
                ]
                child_section_parts.append("**Considerations (short):**\n" + "\n".join(con_lines))

            grandchildren = await db.get_child_questions(child.id)
            if grandchildren:
                page_refs.extend(ref(gc) for gc in grandchildren)
                gc_lines = []
                for gc in grandchildren:
                    gc_summary = await db.get_latest_summary_for_question(gc.id)
                    if gc_summary:
                        page_refs.append(ref(gc_summary))
                    gc_judgements = await db.get_judgements_for_question(gc.id)
                    if gc_judgements:
                        page_refs.extend(ref(j) for j in gc_judgements)
                    gc_hl = await format_page(
                        gc, PageDetail.HEADLINE, track=True, track_tags=summary_tag
                    )
                    gc_medium = (
                        await format_page(
                            gc_summary,
                            PageDetail.ABSTRACT,
                            linked_detail=None,
                            db=db,
                            track=True,
                            track_tags=summary_tag,
                        )
                        if gc_summary
                        else None
                    )
                    gc_short_j = (
                        await format_page(
                            max(gc_judgements, key=lambda j: j.created_at),
                            PageDetail.HEADLINE,
                            track=True,
                            track_tags=summary_tag,
                        )
                        if gc_judgements
                        else None
                    )
                    gc_lines.append(
                        f"  - {gc_hl}"
                        + (f"\n    Summary: {gc_medium}" if gc_medium else "")
                        + (f"\n    Judgement: {gc_short_j}" if gc_short_j else "")
                    )
                child_section_parts.append("**Grandchild questions:**\n" + "\n".join(gc_lines))

            child_parts.append("\n\n".join(child_section_parts))

        parts.append(_section("Child Questions", "\n\n---\n\n".join(child_parts)))

    return "\n\n".join(parts), page_refs


async def _supersede_old_summaries(question_id: str, new_summary_id: str, db: DB) -> None:
    """Mark any existing summary pages for this question as superseded."""
    links = await db.get_links_to(question_id)
    for link in links:
        if link.link_type != LinkType.SUMMARIZES:
            continue
        old = await db.get_page(link.from_page_id)
        if (
            old
            and old.is_active()
            and old.page_type == PageType.SUMMARY
            and old.id != new_summary_id
        ):
            await db.supersede_page(old.id, new_summary_id)
            log.info("Superseded old summary %s with %s", old.id[:8], new_summary_id[:8])


async def summarize_question(
    question_id: str,
    db: DB,
    parent_call_id: str | None = None,
    sequence_id: str | None = None,
    sequence_position: int | None = None,
) -> str | None:
    """Generate a summary page for a question subtree. Free (not budget-counted).

    Returns the new summary page ID, or None on failure.
    """
    log.info("summarize_question: question=%s", question_id[:8])

    call = await db.create_call(
        CallType.SUMMARIZE,
        scope_page_id=question_id,
        parent_call_id=parent_call_id,
        sequence_id=sequence_id,
        sequence_position=sequence_position,
    )
    trace = CallTrace(call.id, db)
    set_trace(trace)

    with page_track_scope(
        call_type=CallType.SUMMARIZE.value,
        question=question_id[:8],
    ):
        try:
            context, context_page_ids = await _build_summary_context(question_id, db)
            await trace.record(
                ContextBuiltEvent(
                    working_context_page_ids=context_page_ids,
                )
            )
            user_message = build_user_message(context, TASK)

            meta = LLMExchangeMetadata(call_id=call.id, phase="summarize")
            result = await structured_call(
                system_prompt=SYSTEM_PROMPT,
                user_message=user_message,
                response_model=SummaryOutput,
                metadata=meta,
                db=db,
            )

            if not result.parsed:
                log.warning(
                    "summarize_question: structured call returned no data for %s",
                    question_id[:8],
                )
                await _fail_call(call, db)
                return None

            data = result.parsed
            question = await db.get_page(question_id)
            page_headline = (
                data.page_headline
                or f"Summary of {question.headline[:60] if question else question_id[:8]}"
            )

            page = Page(
                page_type=PageType.SUMMARY,
                layer=PageLayer.SQUIDGY,
                workspace=Workspace.RESEARCH,
                content=data.content,
                headline=data.headline or page_headline,
                abstract=data.abstract,
                robustness=2,
                robustness_reasoning=(
                    "Auto-generated subtree summary — robustness could be "
                    "strengthened by cross-checking the cited judgements and "
                    "resolving any conflicting subtree conclusions."
                ),
                provenance_model=get_settings().model,
                provenance_call_type=CallType.SUMMARIZE.value,
                provenance_call_id=call.id,
            )
            await db.save_page(page)

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
            call.result_summary = f"Summary created: {page_headline[:80]}"
            await db.save_call(call)

            log.info("summarize_question complete: page=%s", page.id[:8])
            return page.id

        except Exception as e:
            log.error(
                "summarize_question failed for %s: %s",
                question_id[:8],
                e,
                exc_info=True,
            )
            await trace.record(
                ErrorEvent(
                    message=f"Summarize failed: {e}",
                    phase="summarize",
                )
            )
            await _fail_call(call, db)
            return None

        finally:
            await trace.flush_page_loads()


async def _fail_call(call: Call, db: DB) -> None:
    call.status = CallStatus.FAILED
    call.completed_at = datetime.now(UTC)
    await db.save_call(call)
