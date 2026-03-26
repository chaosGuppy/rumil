"""
Multi-stage research report generation.

Pipeline: outliner -> section writer -> integrator -> rendered markdown.
"""

import json
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from rumil.database import DB
from rumil.llm import structured_call, text_call
from rumil.summary import build_research_tree

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent.parent.parent / "prompts"
REPORTS_DIR = Path(__file__).parent.parent.parent / "pages" / "reports"


def _load_prompt(name: str) -> str:
    path = PROMPTS_DIR / name
    return path.read_text(encoding="utf-8") if path.exists() else ""


class OutlineSection(BaseModel):
    sequence: int
    title: str
    description: str
    key_takeaway: str
    source_judgements: Sequence[str] = []
    source_considerations: Sequence[str] = []
    source_questions: Sequence[str] = []
    additional_pages: Sequence[str] = []
    importance: int
    notes_for_drafter: str = ""


class ExcludedFinding(BaseModel):
    description: str
    reason: str


class SuggestedAppendix(BaseModel):
    title: str
    description: str
    source_pages: Sequence[str] = []


class ReportOutline(BaseModel):
    title: str
    executive_summary_sketch: str
    sections: Sequence[OutlineSection]
    excluded_findings: Sequence[ExcludedFinding] = []
    suggested_appendices: Sequence[SuggestedAppendix] = []
    outline_concerns: str = ""


class ConfidenceNote(BaseModel):
    claim: str
    confidence: int
    support_type: str
    comment: str = ""


class KeyDependency(BaseModel):
    claim: str
    depends_on: str
    how_load_bearing: str


class SectionDraft(BaseModel):
    sequence: int
    title: str
    content: str
    confidence_notes: Sequence[ConfidenceNote] = []
    key_dependencies: Sequence[KeyDependency] = []
    flags_for_integrator: str = ""



async def _run_outliner(research_tree: str) -> ReportOutline:
    system_prompt = _load_prompt("report-outliner.md")
    user_message = (
        "Here is the full research tree:\n\n"
        f"{research_tree}"
    )
    result = await structured_call(
        system_prompt=system_prompt,
        user_message=user_message,
        response_model=ReportOutline,
    )
    if not result.data:
        raise RuntimeError("Outliner returned no data")
    return ReportOutline.model_validate(result.data)


async def _collect_section_pages(
    section: OutlineSection, db: DB
) -> str:
    all_ids: list[str] = []
    all_ids.extend(section.source_judgements)
    all_ids.extend(section.source_considerations)
    all_ids.extend(section.source_questions)
    all_ids.extend(section.additional_pages)

    if not all_ids:
        return "(No source pages specified for this section.)"

    resolved_ids: list[str] = []
    for pid in all_ids:
        full_id = await db.resolve_page_id(pid)
        if full_id:
            resolved_ids.append(full_id)
        else:
            log.warning("Could not resolve page ID: %s", pid)

    if not resolved_ids:
        return "(No source pages could be resolved.)"

    pages = await db.get_pages_by_ids(resolved_ids)
    parts: list[str] = []
    for pid in resolved_ids:
        page = pages.get(pid)
        if page:
            parts.append(
                f"### {page.page_type.value}: {page.headline}\n"
                f"ID: {pid[:8]}\n\n"
                f"{page.content}\n"
            )
    return "\n".join(parts) if parts else "(No source pages found.)"


async def _run_section_writer(
    outline: ReportOutline,
    section: OutlineSection,
    section_pages_text: str,
) -> SectionDraft:
    system_prompt = _load_prompt("report-section-writer.md")

    outline_summary = json.dumps(
        outline.model_dump(mode="json"),
        indent=2,
    )
    section_spec = json.dumps(
        section.model_dump(mode="json"),
        indent=2,
    )

    user_message = (
        "## Report Outline\n\n"
        f"{outline_summary}\n\n"
        "---\n\n"
        "## Your Section Specification\n\n"
        f"{section_spec}\n\n"
        "---\n\n"
        "## Source Pages\n\n"
        f"{section_pages_text}"
    )
    result = await structured_call(
        system_prompt=system_prompt,
        user_message=user_message,
        response_model=SectionDraft,
    )
    if not result.data:
        raise RuntimeError(f"Section writer returned no data for section {section.sequence}")
    return SectionDraft.model_validate(result.data)


async def _run_integrator(
    outline: ReportOutline,
    drafts: Sequence[SectionDraft],
) -> str:
    system_prompt = _load_prompt("report-integrator.md")

    outline_summary = json.dumps(
        outline.model_dump(mode="json"),
        indent=2,
    )

    drafts_text_parts: list[str] = []
    for draft in drafts:
        drafts_text_parts.append(
            f"## Section {draft.sequence}: {draft.title}\n\n"
            f"{draft.content}\n\n"
            f"**Flags for integrator:** {draft.flags_for_integrator or '(none)'}\n"
        )
    drafts_text = "\n---\n\n".join(drafts_text_parts)

    user_message = (
        "## Report Outline\n\n"
        f"{outline_summary}\n\n"
        "---\n\n"
        "## Section Drafts\n\n"
        f"{drafts_text}"
    )
    return await text_call(
        system_prompt=system_prompt,
        user_message=user_message,
    )



async def generate_report(
    question_id: str,
    db: DB,
    max_depth: int = 4,
) -> str:
    question = await db.get_page(question_id)
    if not question:
        raise ValueError(f"Question {question_id} not found")

    log.info("Building research tree...")
    research_tree = await build_research_tree(
        question_id, db, max_depth=max_depth, summary_cutoff=max_depth
    )
    if not research_tree.strip():
        return f"No research found for question: {question.headline}"

    log.info("Running outliner...")
    outline = await _run_outliner(research_tree)
    log.info("Outline complete: %d sections planned", len(outline.sections))

    log.info("Drafting sections...")
    drafts: list[SectionDraft] = []
    for section in sorted(outline.sections, key=lambda s: s.sequence):
        log.info("  Drafting section %d: %s", section.sequence, section.title)
        section_pages = await _collect_section_pages(section, db)
        draft = await _run_section_writer(outline, section, section_pages)
        drafts.append(draft)

    log.info("Running integrator...")
    return await _run_integrator(outline, drafts)


def save_report(report_text: str, question_headline: str) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    slug = "".join(c if c.isalnum() or c in " -" else "" for c in question_headline[:50])
    slug = slug.strip().replace(" ", "-").lower()
    filename = f"{timestamp}-{slug}.md"
    path = REPORTS_DIR / filename
    path.write_text(report_text, encoding="utf-8")
    return path
