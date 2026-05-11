"""
Drive the generative pipeline (spec → refine → artefact) once per memo
candidate identified by the scanner.

The artefact-task question's content is intentionally clean: original
investigation question + memo task with topic ~= candidate.title +
freedom-to-contradict. The candidate's other fields (headline_claim,
content_guess, importance, surprise, why_*, epistemic_signals) are exposed
to the spec writer (and refiner) via custom context builders that append a
"scanner brief" + rendered source pages on top of the normal context.

Each candidate gets its own MemoOrchestrator run; each artefact lands as
both a workspace ARTEFACT page (via the existing pipeline) and a markdown
file under pages/memos/.
"""

import asyncio
import logging
import re
from collections.abc import Sequence
from pathlib import Path

from rumil.calls.context_builders import RefinementContext
from rumil.calls.generate_spec import GenerateSpecCall
from rumil.calls.refine_spec import RefineSpecCall
from rumil.calls.stages import CallInfra, ContextBuilder, ContextResult
from rumil.context import format_page
from rumil.database import DB
from rumil.llm import text_call
from rumil.memo_mode import memo_mode
from rumil.memos import MemoCandidate, MemoScan
from rumil.models import (
    CallType,
    LinkType,
    Page,
    PageDetail,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)
from rumil.orchestrators.generative import GenerativeOrchestrator, GenerativeResult
from rumil.prompts import PROMPTS_DIR
from rumil.settings import get_settings

log = logging.getLogger(__name__)

MEMOS_DIR = Path(__file__).parent.parent.parent / "pages" / "memos"


def _build_task_content(original_question: Page, candidate: MemoCandidate) -> str:
    """Render the artefact-task question content seen by everyone in the pipeline.

    Intentionally does NOT include the scanner's other candidate fields
    (headline_claim, content_guess, etc.) — those are visible to the spec
    writer separately via the custom context builder. The general memo
    guidance (length, voice, hypothesis-vs-confident-claim handling) lives
    here so the artefact writer and critics see it directly, not just via
    spec items the spec writer happens to encode.
    """
    return (
        "# Original investigation question\n\n"
        f"**{original_question.headline}**\n\n"
        f"{original_question.content}\n\n"
        "---\n\n"
        "# Your task\n\n"
        "Write a brief memo for the original asker of this question, "
        "highlighting something interesting, important, or surprising that "
        "came out of the investigation. The memo should approximate the "
        f'topic: "{candidate.title}".\n\n'
        "You are free to contradict, reshape, or sharpen this title if a "
        "different framing seems more correct as you produce the memo. The "
        "title is a starting point, not a binding requirement.\n\n"
        "---\n\n"
        "# Memo guidance\n\n"
        "Aim for around 500 words (250–1000 is fine), as though this was "
        "written by Toby Ord.\n\n"
        "You are writing for a human researcher who does not have access to "
        "the investigation or workspace. They just want to know interesting "
        "things that are relevant to the original question. Provide them "
        "with that information.\n\n"
        "The reader should be able to tell from the opening — title and "
        "first paragraph — what the memo is about, in concrete terms a "
        "reader new to the topic can grasp. Define key terms the first "
        "time they appear, especially anything that sounds like jargon or "
        "that the investigation used as shorthand. Assume the reader has "
        "only what is on the page in front of them.\n\n"
        "If you are offering something which is a hypothesis, be clear "
        "about that, and briefly provide the strongest reasons for and "
        "against it. If you are offering a confident claim, walk the "
        "reader through the argument.\n\n"
        "Do not lead with speculative numerical estimates. Numbers like "
        '"20–30% probability" or "60/40 weighting" are useful only '
        "when you can show the Fermi-style calculation behind them — the "
        "components, their ranges, and how they combine — so the reader "
        "can argue with the reasoning rather than the figure. If you "
        "cannot show the calculation, omit the number; a qualitative "
        "statement that conveys the same direction is more honest."
    )


def _render_scanner_brief(candidate: MemoCandidate) -> str:
    """Render the candidate fields as input visible to the spec writer.

    Framed as input, not as a binding requirement — the spec writer is free
    to depart from the brief if a different framing serves the asker better.
    """
    return (
        "## Scanner brief on this candidate\n\n"
        "A prior scanner identified this finding as memo-worthy and produced "
        "the brief below. Treat it as input — it informs your spec but does "
        "not bind it. Your job is to write a spec rich enough that a "
        "downstream writer (who will see only the spec) can produce a "
        "memo that stands alone for a sceptical reader.\n\n"
        f"**Headline claim:** {candidate.headline_claim}\n\n"
        f"**Content sketch:** {candidate.content_guess}\n\n"
        f"**Importance:** {candidate.importance}/5 — {candidate.why_important}\n\n"
        f"**Surprise:** {candidate.surprise}/5 — {candidate.why_surprising}\n\n"
        "**Epistemic signals (what the memo must convey about reliability):** "
        f"{candidate.epistemic_signals}\n"
    )


async def _render_source_pages(
    page_ids: Sequence[str],
    db: DB,
) -> str:
    """Fetch and render the pages the scanner flagged as relevant context."""
    if not page_ids:
        return ""

    resolved_map = await db.resolve_page_ids(page_ids)
    full_ids: list[str] = []
    for pid in page_ids:
        full = resolved_map.get(pid)
        if full:
            full_ids.append(full)
        else:
            log.warning("memo drafter: could not resolve page id %s", pid)
    if not full_ids:
        return ""

    pages = await db.get_pages_by_ids(full_ids)
    parts: list[str] = ["## Source pages identified by the scanner\n"]
    for pid in full_ids:
        page = pages.get(pid)
        if page is None:
            continue
        rendered = await format_page(
            page,
            PageDetail.CONTENT,
            db=db,
        )
        parts.append(f"### `{pid[:8]}` — {page.page_type.value}: {page.headline}\n\n{rendered}\n")
    return "\n".join(parts)


def _append_brief(text: str, brief: str, source_pages_text: str) -> str:
    """Append the scanner brief and source pages to a context_text block."""
    parts = [text.rstrip(), "\n\n---\n\n", brief]
    if source_pages_text:
        parts.append("\n\n")
        parts.append(source_pages_text)
    return "".join(parts)


class MemoGenerateSpecContext(ContextBuilder):
    """generate_spec context for memo runs.

    Skips the workspace embedding sweep entirely. The spec writer sees:
    artefact-task content (original question + memo task + memo guidance),
    the scanner brief on this candidate, and the rendered source pages
    the scanner identified as relevant.
    """

    def __init__(
        self,
        call_type: CallType,
        *,
        brief: str,
        source_pages_text: str,
    ) -> None:
        self._call_type = call_type
        self._brief = brief
        self._source_pages_text = source_pages_text

    async def build_context(self, infra: CallInfra) -> ContextResult:
        task = await infra.db.get_page(infra.question_id)
        task_section = (
            f"# Artefact task\n\n{task.headline}\n\n{task.content or '(no further description)'}"
            if task is not None
            else ""
        )
        parts = [task_section, "\n\n---\n\n", self._brief]
        if self._source_pages_text:
            parts.append("\n\n")
            parts.append(self._source_pages_text)
        return ContextResult(
            context_text="".join(parts),
            working_page_ids=[task.id] if task is not None else [],
            preloaded_ids=[],
        )


class MemoRefineSpecContext(RefinementContext):
    """refine_spec context for memo runs: normal refinement context + brief + source pages."""

    def __init__(
        self,
        *,
        brief: str,
        source_pages_text: str,
    ) -> None:
        super().__init__()
        self._brief = brief
        self._source_pages_text = source_pages_text

    async def build_context(self, infra: CallInfra) -> ContextResult:
        result = await super().build_context(infra)
        result.context_text = _append_brief(
            result.context_text,
            self._brief,
            self._source_pages_text,
        )
        return result


class MemoGenerateSpecCall(GenerateSpecCall):
    """generate_spec for memo runs — uses MemoGenerateSpecContext."""

    def __init__(
        self,
        *args,
        brief: str,
        source_pages_text: str,
        **kwargs,
    ) -> None:
        self._brief = brief
        self._source_pages_text = source_pages_text
        super().__init__(*args, **kwargs)

    def _make_context_builder(self) -> ContextBuilder:
        return MemoGenerateSpecContext(
            self.call_type,
            brief=self._brief,
            source_pages_text=self._source_pages_text,
        )


class MemoRefineSpecCall(RefineSpecCall):
    """refine_spec for memo runs — uses MemoRefineSpecContext."""

    def __init__(
        self,
        *args,
        brief: str,
        source_pages_text: str,
        **kwargs,
    ) -> None:
        self._brief = brief
        self._source_pages_text = source_pages_text
        super().__init__(*args, **kwargs)

    def _make_context_builder(self) -> ContextBuilder:
        return MemoRefineSpecContext(
            brief=self._brief,
            source_pages_text=self._source_pages_text,
        )


class MemoOrchestrator(GenerativeOrchestrator):
    """Generative orchestrator that uses memo-aware spec/refine calls.

    The artefact-task question content stays clean (original question +
    memo task + topic ~= candidate.title); the brief + source pages are
    surfaced to the spec writer and refiner via the custom context builders.

    Wraps run() and resume() in `memo_mode(source_pages_text=...)` so the
    rest of the pipeline can opt out of behaviours that don't pay their
    way for memos: closing reviews are skipped, and CritiqueContext uses
    the same scanner-supplied source pages instead of a workspace
    embedding sweep on each critique cycle.
    """

    def __init__(
        self,
        db: DB,
        *,
        brief: str,
        source_pages_text: str,
        refine_max_rounds: int = 10,
        broadcaster=None,
    ) -> None:
        super().__init__(
            db,
            refine_max_rounds=refine_max_rounds,
            broadcaster=broadcaster,
        )
        self._brief = brief
        self._source_pages_text = source_pages_text

    async def run(self, request: str, *, headline: str | None = None) -> GenerativeResult:
        with memo_mode(source_pages_text=self._source_pages_text):
            return await super().run(request, headline=headline)

    async def resume(self, task_id: str) -> GenerativeResult:
        with memo_mode(source_pages_text=self._source_pages_text):
            return await super().resume(task_id)

    async def _run_generate_spec(self, task_id: str) -> None:
        if not await self.db.consume_budget(1):
            log.warning("Budget exhausted before memo generate_spec could run")
            return
        call = await self.db.create_call(
            CallType.GENERATE_SPEC,
            scope_page_id=task_id,
        )
        runner = MemoGenerateSpecCall(
            task_id,
            call,
            self.db,
            broadcaster=self.broadcaster,
            brief=self._brief,
            source_pages_text=self._source_pages_text,
        )
        await runner.run()

    async def _run_refine_spec(self, task_id: str) -> None:
        if not await self.db.consume_budget(1):
            log.warning("Budget exhausted before memo refine_spec could run")
            return
        call = await self.db.create_call(
            CallType.REFINE_SPEC,
            scope_page_id=task_id,
        )
        runner = MemoRefineSpecCall(
            task_id,
            call,
            self.db,
            max_rounds=self.refine_max_rounds,
            broadcaster=self.broadcaster,
            brief=self._brief,
            source_pages_text=self._source_pages_text,
        )
        await runner.run()


def _slugify(text: str, max_len: int = 60) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9 -]+", "", text)[:max_len].strip()
    return cleaned.replace(" ", "-").lower() or "memo"


def save_memo_to_disk(
    artefact: Page,
    candidate: MemoCandidate,
    root_question_id: str,
    root_question_headline: str,
    candidate_index: int,
) -> Path:
    """Write the artefact as a markdown file under pages/memos/{root_short}/.

    Filename pattern: {NN}-{slugified-title}.md, where NN is the 1-based
    candidate index in the scan. A small metadata block at the top records
    the source candidate and originating investigation for traceability.
    """
    root_dir = MEMOS_DIR / root_question_id[:8]
    root_dir.mkdir(parents=True, exist_ok=True)
    slug = _slugify(candidate.title)
    filename = f"{candidate_index:02d}-{slug}.md"
    path = root_dir / filename

    metadata = (
        f'<!-- Drafted from candidate "{candidate.title}" '
        f"(importance {candidate.importance}/5, surprise {candidate.surprise}/5).\n"
        f"     Investigation: {root_question_headline}\n"
        f"     Source pages: {', '.join(candidate.relevant_page_ids) or '(none)'}\n"
        f"     Artefact headline: {artefact.headline}\n"
        f"     Artefact page: {artefact.id} -->\n\n"
    )
    content = artefact.content.strip()
    body = content if content.lstrip().startswith("#") else f"# {artefact.headline}\n\n{content}"
    path.write_text(f"{metadata}{body}\n", encoding="utf-8")
    return path


async def draft_memo_for_candidate(
    candidate: MemoCandidate,
    root_question_id: str,
    db: DB,
    *,
    refine_max_rounds: int = 10,
) -> GenerativeResult:
    """Drive one MemoOrchestrator run for a single candidate.

    Caller is responsible for budget. Returns the orchestrator's result —
    the artefact_id can be looked up via db.get_page().
    """
    root_question = await db.get_page(root_question_id)
    if root_question is None:
        raise ValueError(f"Root question {root_question_id} not found")

    brief = _render_scanner_brief(candidate)
    source_pages_text = await _render_source_pages(candidate.relevant_page_ids, db)
    request = _build_task_content(root_question, candidate)
    headline = f"Memo: {candidate.title[:80]}"

    orchestrator = MemoOrchestrator(
        db,
        brief=brief,
        source_pages_text=source_pages_text,
        refine_max_rounds=refine_max_rounds,
    )
    return await orchestrator.run(request, headline=headline)


async def draft_memos_from_scan(
    scan: MemoScan,
    db: DB,
    *,
    indices: Sequence[int] | None = None,
    refine_max_rounds: int = 10,
) -> list[tuple[MemoCandidate, GenerativeResult, Path | None]]:
    """Draft memos for the candidates in *scan*.

    *indices* (1-based, matching the on-screen candidate ranking from
    render_scan_summary) selects a subset; default is all candidates in
    the order they appear in *scan*.

    Returns a list of (candidate, generative_result, file_path) tuples.
    file_path is None if the artefact wasn't produced or written.
    """
    if not scan.root_question_id:
        raise ValueError(
            "scan has no root_question_id; re-run the scanner to populate it "
            "(older scan files may pre-date this field)"
        )

    candidates = list(scan.candidates)
    if indices is not None:
        chosen: list[tuple[int, MemoCandidate]] = []
        for i in indices:
            if i < 1 or i > len(candidates):
                raise ValueError(
                    f"memo index {i} out of range — scan has {len(candidates)} candidates"
                )
            chosen.append((i, candidates[i - 1]))
    else:
        chosen = list(enumerate(candidates, start=1))

    async def _draft_one(
        i: int,
        candidate: MemoCandidate,
    ) -> tuple[MemoCandidate, GenerativeResult | None, Path | None, Exception | None]:
        log.info("Drafting memo %d: %s", i, candidate.title[:60])
        try:
            result = await draft_memo_for_candidate(
                candidate,
                scan.root_question_id,
                db,
                refine_max_rounds=refine_max_rounds,
            )
        except Exception as exc:
            log.exception("Memo %d failed: %s", i, candidate.title[:60])
            return candidate, None, None, exc
        path: Path | None = None
        if result.artefact_id:
            artefact = await db.get_page(result.artefact_id)
            if artefact is not None:
                path = save_memo_to_disk(
                    artefact,
                    candidate,
                    scan.root_question_id,
                    scan.root_question_headline,
                    i,
                )
        return candidate, result, path, None

    gathered = await asyncio.gather(*[_draft_one(i, c) for i, c in chosen])

    # The historical return shape was (candidate, result, path); preserve that
    # for callers, dropping the per-task exception (already logged above).
    results: list[tuple[MemoCandidate, GenerativeResult, Path | None]] = []
    for candidate, result, path, _exc in gathered:
        if result is not None:
            results.append((candidate, result, path))
    return results


def load_scan_from_path(path: Path) -> MemoScan:
    """Read a saved MemoScan JSON file and validate it."""
    import json

    payload = json.loads(path.read_text(encoding="utf-8"))
    return MemoScan.model_validate(payload)


async def generate_memo_summary(
    scan: MemoScan,
    drafted: Sequence[tuple[MemoCandidate, GenerativeResult, Path | None]],
    db: DB,
) -> str:
    """Produce a summary index over the drafted memos.

    One paragraph per memo, framed for a reader who has not read any of
    the memos yet. Single text_call — does not consume budget.
    """
    drafted_with_artefacts: list[tuple[MemoCandidate, Page]] = []
    for candidate, result, _path in drafted:
        if not result.artefact_id:
            continue
        artefact = await db.get_page(result.artefact_id)
        if artefact is not None:
            drafted_with_artefacts.append((candidate, artefact))

    if not drafted_with_artefacts:
        return "No memos were drafted."

    memo_blocks: list[str] = []
    for i, (_candidate, artefact) in enumerate(drafted_with_artefacts, start=1):
        memo_blocks.append(f"### Memo {i}: {artefact.headline}\n\n{artefact.content.strip()}")

    user_message = (
        "## Original investigation question\n\n"
        f"**{scan.root_question_headline}**\n\n"
        "---\n\n"
        f"## Memos to summarise ({len(drafted_with_artefacts)})\n\n"
        + "\n\n---\n\n".join(memo_blocks)
    )

    system_prompt = (PROMPTS_DIR / "memo-summary.md").read_text(encoding="utf-8")
    return await text_call(system_prompt=system_prompt, user_message=user_message)


def save_memo_summary(
    text: str,
    root_question_id: str,
    root_question_headline: str,
) -> Path:
    """Write the summary index as 00-summary.md in the per-investigation memo dir."""
    root_dir = MEMOS_DIR / root_question_id[:8]
    root_dir.mkdir(parents=True, exist_ok=True)
    path = root_dir / "00-summary.md"
    metadata = f"<!-- Memo summary index for investigation: {root_question_headline} -->\n\n"
    body = text.strip()
    path.write_text(f"{metadata}{body}\n", encoding="utf-8")
    return path


async def publish_memo_index(
    summary_text: str,
    scan: MemoScan,
    drafted: Sequence[tuple[MemoCandidate, GenerativeResult, Path | None]],
    db: DB,
) -> str | None:
    """Publish the summary index as a SUMMARY page linked to the question and memos.

    Creates a single SUMMARY page whose content is the index text, links it
    to the root question via SUMMARIZES, and links it to each successfully
    drafted memo via RELATED. Returns the new page id, or None if there are
    no drafted memos to index.
    """
    drafted_artefact_ids = [
        (candidate, result.artefact_id)
        for candidate, result, _path in drafted
        if result.artefact_id
    ]
    if not drafted_artefact_ids:
        return None

    headline = f"Memo index: {scan.root_question_headline[:80]}"
    page = Page(
        page_type=PageType.SUMMARY,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=summary_text.strip(),
        headline=headline,
        provenance_model=get_settings().model,
        provenance_call_type="memo_summary",
    )
    await db.save_page(page)

    await db.save_link(
        PageLink(
            from_page_id=page.id,
            to_page_id=scan.root_question_id,
            link_type=LinkType.SUMMARIZES,
            reasoning="Memo index over the drafted memos for this investigation",
        )
    )
    for candidate, artefact_id in drafted_artefact_ids:
        await db.save_link(
            PageLink(
                from_page_id=page.id,
                to_page_id=artefact_id,
                link_type=LinkType.RELATED,
                reasoning=f"Indexes memo: {candidate.title[:80]}",
            )
        )

    return page.id
