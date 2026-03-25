"""ContextBuilder implementations for all call types."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from rumil.calls.common import (
    _format_loaded_pages,
    _run_phase1,
    resolve_page_refs,
    run_single_call,
)
from rumil.calls.stages import CallInfra, ContextBuilder, ContextResult
from rumil.context import (
    build_call_context,
    build_embedding_based_context,
    build_scout_context,
    format_page,
    format_preloaded_pages,
)
from rumil.database import DB
from rumil.llm import build_system_prompt, build_user_message
from rumil.models import (
    Call,
    CallType,
    MoveType,
    Page,
    PageDetail,
    FindConsiderationsMode,
)
from rumil.moves.base import MoveState
from rumil.moves.registry import MOVES
from rumil.page_graph import PageGraph
from rumil.tracing.trace_events import ContextBuiltEvent
from rumil.workspace_map import build_workspace_map

log = logging.getLogger(__name__)


async def _record_context_built(
    infra: CallInfra,
    working_page_ids: list[str],
    preloaded_ids: list[str],
    *,
    source_page_id: str | None = None,
    scout_mode: str | None = None,
) -> None:
    await infra.trace.record(
        ContextBuiltEvent(
            working_context_page_ids=await resolve_page_refs(
                working_page_ids, infra.db
            ),
            preloaded_page_ids=await resolve_page_refs(preloaded_ids, infra.db),
            source_page_id=source_page_id,
            scout_mode=scout_mode,
        )
    )


async def _do_phase1(
    infra: CallInfra,
    call_type: CallType,
    context_text: str,
) -> tuple[str, list[str]]:
    """Run phase1 page loading and return (updated context_text, phase1_ids)."""
    system_prompt = build_system_prompt(call_type.value)
    phase1_ids = await _run_phase1(
        system_prompt,
        context_text,
        infra.call.id,
        infra.state,
        infra.db,
    )
    if phase1_ids:
        extra_text = await _format_loaded_pages(phase1_ids, infra.db)
        context_text += "\n\n## Loaded Pages\n\n" + extra_text
    return context_text, phase1_ids


class GraphContextWithPhase1(ContextBuilder):
    """Graph-based context with phase1 page loading. Used by AssessCall."""

    def __init__(self, call_type: CallType) -> None:
        self._call_type = call_type

    async def build_context(self, infra: CallInfra) -> ContextResult:
        preloaded_ids = infra.call.context_page_ids or []
        graph = await PageGraph.load(infra.db)
        context_text, _, working_page_ids = await build_call_context(
            infra.question_id,
            infra.db,
            extra_page_ids=preloaded_ids,
            graph=graph,
        )
        await _record_context_built(infra, working_page_ids, preloaded_ids)
        context_text, phase1_ids = await _do_phase1(
            infra,
            self._call_type,
            context_text,
        )
        return ContextResult(
            context_text=context_text,
            working_page_ids=working_page_ids,
            preloaded_ids=preloaded_ids,
            phase1_ids=phase1_ids,
        )


class EmbeddingContext(ContextBuilder):
    """Embedding-based context (no phase1). Used by embedding variants."""

    def __init__(self, call_type: CallType) -> None:
        self._call_type = call_type

    async def build_context(self, infra: CallInfra) -> ContextResult:
        question = await infra.db.get_page(infra.question_id)
        query = question.headline if question else infra.question_id
        result = await build_embedding_based_context(
            query,
            infra.db,
            scope_question_id=infra.question_id,
        )
        working_page_ids = result.page_ids
        await _record_context_built(infra, working_page_ids, [])
        return ContextResult(
            context_text=result.context_text,
            working_page_ids=working_page_ids,
        )


class IngestGraphContext(ContextBuilder):
    """Graph-based context with source document section. Used by IngestCall."""

    def __init__(self, source_page: Page) -> None:
        self._source_page = source_page
        extra = source_page.extra or {}
        self._filename = extra.get("filename", source_page.id[:8])

    async def build_context(self, infra: CallInfra) -> ContextResult:
        preloaded_ids = infra.call.context_page_ids or []
        graph = await PageGraph.load(infra.db)
        question_context, _, working_page_ids = await build_call_context(
            infra.question_id,
            infra.db,
            extra_page_ids=preloaded_ids,
            graph=graph,
        )
        await _record_context_built(
            infra,
            working_page_ids,
            preloaded_ids,
            source_page_id=self._source_page.id,
        )

        source_section = (
            "\n\n---\n\n## Source Document\n\n"
            f"**File:** {self._filename}  \n"
            f"**Source page ID:** `{self._source_page.id}`\n\n"
            f"{self._source_page.content}"
        )
        context_text = question_context + source_section
        context_text, phase1_ids = await _do_phase1(
            infra,
            CallType.INGEST,
            context_text,
        )
        return ContextResult(
            context_text=context_text,
            working_page_ids=working_page_ids,
            preloaded_ids=preloaded_ids,
            phase1_ids=phase1_ids,
        )


class IngestEmbeddingContext(ContextBuilder):
    """Embedding-based context with source document. Used by EmbeddingIngestCall."""

    def __init__(self, source_page: Page) -> None:
        self._source_page = source_page
        extra = source_page.extra or {}
        self._filename = extra.get("filename", source_page.id[:8])

    async def build_context(self, infra: CallInfra) -> ContextResult:
        question = await infra.db.get_page(infra.question_id)
        query = question.headline if question else infra.question_id
        result = await build_embedding_based_context(
            query,
            infra.db,
            scope_question_id=infra.question_id,
        )
        working_page_ids = result.page_ids
        await _record_context_built(
            infra,
            working_page_ids,
            [],
            source_page_id=self._source_page.id,
        )

        source_section = (
            "\n\n---\n\n## Source Document\n\n"
            f"**File:** {self._filename}  \n"
            f"**Source page ID:** `{self._source_page.id}`\n\n"
            f"{self._source_page.content}"
        )
        return ContextResult(
            context_text=result.context_text + source_section,
            working_page_ids=working_page_ids,
        )


_LINKING_TASK = (
    "Review the workspace and link relevant existing pages to the scope question.\n\n"
    "For each link, specify a role:\n"
    '- **direct**: "Now I know X, I can immediately update my answer." The page '
    "directly bears on the answer to the scope question \u2014 it is evidence, a "
    "counter-argument, or a partial answer.\n"
    '- **structural**: "Now I know X, I know better what evidence and angles to '
    'consider." The page frames the investigation \u2014 it indicates what to look '
    "for, how to decompose the question, or what dimensions matter.\n\n"
    "Link claims as considerations and sub-questions as child questions.\n\n"
    "Be discerning. Only link pages that genuinely bear on this question \u2014 "
    "tangential or weakly related pages should not be linked. "
    "Do not duplicate any links already shown above. "
    "Create no more than 6 new links, and fewer if fewer are warranted \u2014 "
    "do not force links just to fill a quota.\n\n"
    "Scope question ID: `{question_id}`"
)


async def link_new_pages(
    question_id: str,
    call: Call,
    db: DB,
    state: MoveState,
    context_page_ids: Sequence[str] | None = None,
    graph: PageGraph | None = None,
) -> None:
    """Single LLM call that reviews nearby pages and creates direct/structural links.

    Uses only LINK_CONSIDERATION and LINK_CHILD_QUESTION tools with role fields.
    Free (not counted against budget).

    If *context_page_ids* is provided, those pages are shown as headlines
    instead of the full workspace map.
    """
    source: DB | PageGraph = graph if graph is not None else db
    question = await source.get_page(question_id)
    if not question:
        return

    if context_page_ids:
        page_lines: list[str] = []
        for pid in context_page_ids:
            page = await source.get_page(pid)
            if page and page.id != question_id:
                page_lines.append(await format_page(page, PageDetail.HEADLINE))
        pages_text = "# Nearby Pages\n\n" + "\n".join(page_lines)
    else:
        pages_text, _ = await build_workspace_map(db, graph=graph)

    question_text = await format_page(question, PageDetail.HEADLINE)
    existing_links = await _build_link_inventory(question_id, db, graph=graph)
    working_context = (
        pages_text + "\n\n---\n\n"
        "# Scope Question\n\n" + question_text + "\n\n" + existing_links
    )

    linking_tools = [
        MOVES[MoveType.LINK_CONSIDERATION].bind(state),
        MOVES[MoveType.LINK_CHILD_QUESTION].bind(state),
    ]
    task = _LINKING_TASK.format(question_id=question_id)
    system_prompt = build_system_prompt(CallType.FIND_CONSIDERATIONS.value)
    user_message = build_user_message(working_context, task)

    await run_single_call(
        system_prompt=system_prompt,
        user_message=user_message,
        tools=linking_tools,
        call_id=call.id,
        phase="link_new_pages",
        db=db,
        state=state,
    )


async def _build_link_inventory(
    question_id: str,
    db: DB,
    graph: PageGraph | None = None,
) -> str:
    source: DB | PageGraph = graph if graph is not None else db
    considerations = await source.get_considerations_for_question(question_id)
    children_with_links = await source.get_child_questions_with_links(question_id)

    if not considerations and not children_with_links:
        return "No existing links on the scope question."

    lines = ["### Current Links"]
    for page, link in considerations:
        lines.append(
            f"- [{link.role.value}] consideration: "
            f'"{page.headline}" '
            f"(strength {link.strength:.1f}, link_id: `{link.id}`)"
        )
    for page, link in children_with_links:
        lines.append(
            f"- [{link.role.value}] child_question: "
            f'"{page.headline}" '
            f"(link_id: `{link.id}`)"
        )
    return "\n".join(lines)


class FindConsiderationsGraphContext(ContextBuilder):
    """Scout context with link_new_pages. Used by FindConsiderationsCall."""

    def __init__(
        self,
        mode: FindConsiderationsMode,
        context_page_ids: Sequence[str] | None = None,
    ) -> None:
        self._mode = mode
        self._context_page_ids = context_page_ids

    async def build_context(self, infra: CallInfra) -> ContextResult:
        preloaded_ids = self._context_page_ids or []
        graph = await PageGraph.load(infra.db)
        scout_ctx = await build_scout_context(
            infra.question_id,
            infra.db,
            graph=graph,
        )

        await link_new_pages(
            infra.question_id,
            infra.call,
            infra.db,
            infra.state,
            context_page_ids=scout_ctx.page_ids,
            graph=graph,
        )

        working_page_ids = scout_ctx.page_ids
        context_text = scout_ctx.context_text
        if preloaded_ids:
            context_text += await format_preloaded_pages(
                preloaded_ids,
                infra.db,
                graph=graph,
            )

        await infra.trace.record(
            ContextBuiltEvent(
                working_context_page_ids=await resolve_page_refs(
                    working_page_ids,
                    infra.db,
                ),
                preloaded_page_ids=await resolve_page_refs(preloaded_ids, infra.db),
                scout_mode=self._mode.value,
            )
        )

        return ContextResult(
            context_text=context_text,
            working_page_ids=working_page_ids,
            preloaded_ids=preloaded_ids,
        )


class ScoutEmbeddingContext(ContextBuilder):
    """Scout context without link_new_pages. Used by EmbeddingFindConsiderationsCall."""

    def __init__(self, mode: FindConsiderationsMode) -> None:
        self._mode = mode

    async def build_context(self, infra: CallInfra) -> ContextResult:
        graph = await PageGraph.load(infra.db)
        scout_ctx = await build_scout_context(
            infra.question_id,
            infra.db,
            graph=graph,
        )
        working_page_ids = scout_ctx.page_ids

        await infra.trace.record(
            ContextBuiltEvent(
                working_context_page_ids=await resolve_page_refs(
                    working_page_ids,
                    infra.db,
                ),
                preloaded_page_ids=[],
                scout_mode=self._mode.value,
            )
        )

        return ContextResult(
            context_text=scout_ctx.context_text,
            working_page_ids=working_page_ids,
        )


class ConceptScoutContext(ContextBuilder):
    """Graph context + concept registry + phase1. Used by ScoutConceptsCall."""

    def __init__(self, call_type: CallType) -> None:
        self._call_type = call_type

    async def build_context(self, infra: CallInfra) -> ContextResult:
        preloaded_ids = infra.call.context_page_ids or []
        graph = await PageGraph.load(infra.db)
        context_text, _, working_page_ids = await build_call_context(
            infra.question_id,
            infra.db,
            extra_page_ids=preloaded_ids,
            graph=graph,
        )

        registry = await infra.db.get_concept_registry()
        if registry:
            lines = ["## Concept Registry", ""]
            lines.append(
                "The following concepts have already been proposed (do not re-propose them):"
            )
            lines.append("")
            for concept in registry:
                extra = concept.extra or {}
                stage = extra.get("stage", "proposed")
                score = extra.get("score")
                promoted = extra.get("promoted", False)
                status = (
                    "promoted"
                    if promoted
                    else (f"score={score}" if score is not None else stage)
                )
                lines.append(f"- [{status}] `{concept.id[:8]}` — {concept.headline}")
            context_text += "\n\n" + "\n".join(lines)

        await _record_context_built(infra, working_page_ids, preloaded_ids)
        context_text, phase1_ids = await _do_phase1(
            infra,
            self._call_type,
            context_text,
        )
        return ContextResult(
            context_text=context_text,
            working_page_ids=working_page_ids,
            preloaded_ids=preloaded_ids,
            phase1_ids=phase1_ids,
        )


class ConceptAssessContext(ContextBuilder):
    """Workspace map + concept page + assessment history + phase1.
    Used by AssessConceptCall.
    """

    def __init__(self, phase: str) -> None:
        self._phase = phase

    async def build_context(self, infra: CallInfra) -> ContextResult:
        concept = await infra.db.get_page(infra.question_id)
        if not concept:
            return ContextResult(
                context_text=f"[Concept page {infra.question_id} not found]",
                working_page_ids=[],
            )

        map_text, _ = await build_workspace_map(infra.db)
        concept_text = await format_page(concept, PageDetail.HEADLINE, db=infra.db)

        extra = concept.extra or {}
        assessment_rounds = extra.get("assessment_rounds", [])
        history_section = ""
        if assessment_rounds:
            lines = ["## Previous Assessment Rounds", ""]
            for i, r in enumerate(assessment_rounds):
                lines.append(
                    f"Round {i + 1} ({r.get('phase', '?')}): "
                    f"score={r.get('score', '?')}, "
                    f"remaining_fruit={r.get('remaining_fruit', '?')}"
                )
                if r.get("what_worked"):
                    lines.append(f"  Worked: {r['what_worked']}")
                if r.get("what_didnt"):
                    lines.append(f"  Didn't: {r['what_didnt']}")
            history_section = "\n\n" + "\n".join(lines)

        context_text = (
            "\n\n".join(
                [
                    map_text,
                    "---",
                    "## Concept Under Assessment",
                    "",
                    concept_text,
                ]
            )
            + history_section
        )

        working_page_ids = [infra.question_id]
        preloaded_ids = infra.call.context_page_ids or []
        await infra.trace.record(
            ContextBuiltEvent(
                working_context_page_ids=await resolve_page_refs(
                    working_page_ids,
                    infra.db,
                ),
                preloaded_page_ids=await resolve_page_refs(preloaded_ids, infra.db),
            )
        )
        context_text, phase1_ids = await _do_phase1(
            infra,
            CallType.ASSESS_CONCEPT,
            context_text,
        )
        return ContextResult(
            context_text=context_text,
            working_page_ids=working_page_ids,
            preloaded_ids=preloaded_ids,
            phase1_ids=phase1_ids,
        )


class WebResearchEmbeddingContext(ContextBuilder):
    """Embedding context for web research. Records diagnostic info."""

    async def build_context(self, infra: CallInfra) -> ContextResult:
        question = await infra.db.get_page(infra.question_id)
        query = question.headline if question else infra.question_id
        emb_result = await build_embedding_based_context(
            query,
            infra.db,
            scope_question_id=infra.question_id,
        )
        working_page_ids = emb_result.page_ids
        context_text = emb_result.context_text

        system_prompt = build_system_prompt("web_research")
        user_message = build_user_message(context_text, "(diagnostic)")
        log.debug(
            "Web research context diagnostic: "
            "context_text=%d chars, system_prompt=%d chars, "
            "user_message=%d chars, total_prompt=%d chars, "
            "full_pages=%d, abstract_pages=%d, summary_pages=%d, "
            "distillation_pages=%d, "
            "budget_usage=%s",
            len(context_text),
            len(system_prompt),
            len(user_message),
            len(system_prompt) + len(user_message),
            len(emb_result.full_page_ids),
            len(emb_result.abstract_page_ids),
            len(emb_result.summary_page_ids),
            len(emb_result.distillation_page_ids),
            emb_result.budget_usage,
        )

        await infra.trace.record(
            ContextBuiltEvent(
                working_context_page_ids=await resolve_page_refs(
                    working_page_ids,
                    infra.db,
                ),
                preloaded_page_ids=[],
            )
        )

        return ContextResult(
            context_text=context_text,
            working_page_ids=working_page_ids,
        )
