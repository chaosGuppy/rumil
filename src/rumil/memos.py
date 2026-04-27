"""
Scan a completed investigation for the most important and surprising findings,
and produce candidate memo specs that a downstream drafter can write up.

Output: a MemoScan with ranked candidates (title, content_guess, importance,
surprise, relevant_page_ids, epistemic_signals) plus whole-picture scan_notes.
Persisted as JSON to pages/memo-scans/ so a future drafter can consume it.
"""

import json
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from rumil.database import DB
from rumil.llm import structured_call
from rumil.prompts import PROMPTS_DIR
from rumil.summary import build_research_tree

log = logging.getLogger(__name__)

MEMO_SCANS_DIR = Path(__file__).parent.parent.parent / "pages" / "memo-scans"


def _load_prompt(name: str) -> str:
    path = PROMPTS_DIR / name
    return path.read_text(encoding="utf-8") if path.exists() else ""


class MemoCandidate(BaseModel):
    title: str
    headline_claim: str
    content_guess: str
    importance: int = Field(ge=1, le=5)
    surprise: int = Field(ge=1, le=5)
    why_important: str
    why_surprising: str
    relevant_page_ids: Sequence[str]
    epistemic_signals: str


class ExcludedFinding(BaseModel):
    description: str
    reason: str


class MemoScan(BaseModel):
    scan_notes: str = ""
    candidates: Sequence[MemoCandidate] = ()
    excluded: Sequence[ExcludedFinding] = ()
    root_question_id: str = ""
    root_question_headline: str = ""


async def scan_for_memos(
    question_id: str,
    db: DB,
    max_depth: int = 4,
) -> MemoScan:
    """Run the memo scanner against a completed investigation.

    Returns a MemoScan describing memo-worthy findings. Does not write the
    scan to disk; callers persist via save_memo_scan().
    """
    question = await db.get_page(question_id)
    if not question:
        raise ValueError(f"Question {question_id} not found")

    log.info("Building research tree for memo scan...")
    research_tree = await build_research_tree(
        question_id,
        db,
        max_depth=max_depth,
        summary_cutoff=max_depth,
    )
    if not research_tree.strip():
        return MemoScan(
            scan_notes=f"No research found for question: {question.headline}",
            candidates=(),
            excluded=(),
            root_question_id=question.id,
            root_question_headline=question.headline,
        )

    log.info("Running memo scanner...")
    system_prompt = _load_prompt("memo-scanner.md")
    user_message = (
        "Here is the full body of research on this question:\n\n"
        f"{research_tree}"
    )
    result = await structured_call(
        system_prompt=system_prompt,
        user_message=user_message,
        response_model=MemoScan,
    )
    if not result.parsed:
        raise RuntimeError("Memo scanner returned no data")
    scan = result.parsed
    scan.root_question_id = question.id
    scan.root_question_headline = question.headline
    return scan


def save_memo_scan(scan: MemoScan, question_headline: str) -> Path:
    """Persist a MemoScan as JSON in pages/memo-scans/. Returns the path."""
    MEMO_SCANS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    slug = "".join(c if c.isalnum() or c in " -" else "" for c in question_headline[:50])
    slug = slug.strip().replace(" ", "-").lower()
    filename = f"{timestamp}-{slug}.json"
    path = MEMO_SCANS_DIR / filename
    path.write_text(
        json.dumps(scan.model_dump(mode="json"), indent=2),
        encoding="utf-8",
    )
    return path


def render_scan_summary(scan: MemoScan) -> str:
    """Render a MemoScan as a human-readable digest for stdout."""
    lines: list[str] = []
    if scan.scan_notes:
        lines.append("## Scan notes\n")
        lines.append(scan.scan_notes)
        lines.append("")
    lines.append(f"## Memo candidates ({len(scan.candidates)})\n")
    ranked = sorted(
        scan.candidates,
        key=lambda c: (c.importance + c.surprise, c.importance),
        reverse=True,
    )
    for i, c in enumerate(ranked, start=1):
        lines.append(
            f"### {i}. {c.title}  "
            f"[importance {c.importance}, surprise {c.surprise}]"
        )
        lines.append(f"**Headline:** {c.headline_claim}")
        lines.append("")
        lines.append(c.content_guess)
        lines.append("")
        lines.append(f"*Why important:* {c.why_important}")
        lines.append(f"*Why surprising:* {c.why_surprising}")
        page_list = ", ".join(c.relevant_page_ids) if c.relevant_page_ids else "(none)"
        lines.append(f"*Relevant pages:* {page_list}")
        if c.epistemic_signals:
            lines.append(f"*Epistemic signals:* {c.epistemic_signals}")
        lines.append("")
    if scan.excluded:
        lines.append(f"## Excluded ({len(scan.excluded)})\n")
        for e in scan.excluded:
            lines.append(f"- **{e.description}** — {e.reason}")
    return "\n".join(lines)
