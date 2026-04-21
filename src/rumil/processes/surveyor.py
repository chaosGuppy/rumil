"""Surveyor process: minimal cross-cutting survey of a project or subgraph.

New implementation (not a wrap of ``GlobalPrioOrchestrator``). Builds a
light context sample of the scope's graph, runs one structured LLM
call, and converts the output into:

- a ``MapDelta`` carrying one synthesis View page summarising what the
  surveyor saw, plus any proposed cross-cutting questions (none in v1
  — the LLM only names suggestions, it doesn't create them), and
- a list of ``FollowUp`` signals recommending what should happen next
  (focus, reassess, consolidate, robustify).

The signals are the primary output surface for a Surveyor. The
MapDelta's View holds the surveyor's narrative; the signals hold its
actionable recommendations. A scheduler (future) would consume the
signals; v1 just emits them for observability.
"""

import logging
import time
from collections.abc import Sequence

from pydantic import BaseModel, Field

from rumil.database import DB
from rumil.llm import LLMExchangeMetadata, structured_call
from rumil.models import (
    CallStatus,
    CallType,
    LinkType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)
from rumil.processes.budget import BudgetEnvelope, ResourceUsage
from rumil.processes.delta import LinkRef, MapDelta, PageRef
from rumil.processes.result import Result
from rumil.processes.scope import ProjectScope, SubgraphScope
from rumil.processes.signals import (
    ConsolidateRequest,
    FocusRequest,
    FollowUp,
    ReassessRequest,
    RobustifyRequest,
)
from rumil.tracing.broadcast import Broadcaster
from rumil.tracing.trace_events import ProcessCompletedEvent, ProcessStartedEvent
from rumil.tracing.tracer import CallTrace

log = logging.getLogger(__name__)

SurveyorScope = ProjectScope | SubgraphScope


class _FocusIdea(BaseModel):
    question_id: str = Field(description="Full page ID of a question to focus on")
    priority: float = Field(ge=0, le=1, description="0-1 suggested priority")
    reason: str


class _ReassessIdea(BaseModel):
    page_id: str = Field(description="Full page ID of a judgement/view that seems stale")
    reason: str


class _ConsolidateIdea(BaseModel):
    page_ids: list[str] = Field(description="Near-duplicate pages worth merging")
    reason: str


class _RobustifyIdea(BaseModel):
    claim_id: str = Field(description="Full page ID of a claim worth robustifying")
    reason: str


class _SurveyScanOutput(BaseModel):
    summary: str = Field(
        description=(
            "2-4 short paragraphs summarising the current state of "
            "investigation across the surveyed scope: what's been "
            "established, what seems open, what looks tangled."
        )
    )
    focus: list[_FocusIdea] = Field(
        default_factory=list,
        description="Questions that deserve more investigation budget.",
    )
    reassess: list[_ReassessIdea] = Field(
        default_factory=list,
        description="Judgements or views that look stale given newer evidence.",
    )
    consolidate: list[_ConsolidateIdea] = Field(
        default_factory=list,
        description="Groups of near-duplicate pages worth merging.",
    )
    robustify: list[_RobustifyIdea] = Field(
        default_factory=list,
        description="Claims that would benefit from variant generation.",
    )


SYSTEM_PROMPT = (
    "You are a research surveyor. You look across a research workspace "
    "and identify cross-cutting structure: what threads connect, what "
    "seems stale, what looks like near-duplicate work, and what "
    "directions deserve more investigation. "
    "You do not do the investigation itself — you produce a short "
    "synthesis plus a ranked list of recommendations that a scheduler "
    "or human can act on. "
    "Only reference page IDs that appear in the context provided. Be "
    "conservative — if nothing looks like a genuine duplicate, leave "
    "the consolidate list empty. Same for the other recommendation "
    "types. Quality beats coverage."
)


def _format_pages(pages: Sequence[Page], limit_chars: int) -> str:
    """Compact rendering of a page list for the surveyor's context."""
    lines: list[str] = []
    used = 0
    for p in pages:
        short = p.id[:8]
        head = p.headline.strip() or "(no headline)"
        kind = p.page_type.value
        extra = ""
        if p.credence is not None:
            extra += f" credence={p.credence}"
        if p.robustness is not None:
            extra += f" robustness={p.robustness}"
        line = f"- [{short}] {kind}{extra}: {head}"
        if used + len(line) > limit_chars:
            lines.append("- ... (truncated)")
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


async def _resolve_roots(db: DB, scope: SurveyorScope) -> list[str]:
    if isinstance(scope, SubgraphScope):
        return list(scope.root_ids)
    roots = await db.get_root_questions()
    return [r.id for r in roots]


async def _gather_context_pages(
    db: DB, root_ids: Sequence[str], depth: int, per_root_cap: int = 25
) -> list[Page]:
    """BFS from each root down to *depth*, collecting pages for the surveyor.

    Uses batched level-by-level fetches (see CLAUDE.md: "level-by-level
    BFS instead of recursive per-node fetches"). Dedupes across roots.
    """
    seen: set[str] = set()
    collected: list[Page] = []
    roots = await db.get_pages_by_ids(list(root_ids))
    frontier: list[str] = []
    for rid in root_ids:
        if rid in roots and rid not in seen:
            seen.add(rid)
            collected.append(roots[rid])
            frontier.append(rid)
    for _ in range(depth):
        if not frontier:
            break
        links_map = await db.get_links_from_many(frontier)
        next_ids: list[str] = []
        for _from, links in links_map.items():
            for link in links:
                if link.to_page_id in seen:
                    continue
                seen.add(link.to_page_id)
                next_ids.append(link.to_page_id)
        if not next_ids:
            break
        pages = await db.get_pages_by_ids(next_ids)
        for pid in next_ids:
            if pid in pages:
                collected.append(pages[pid])
                if len(collected) >= per_root_cap * max(len(root_ids), 1):
                    return collected
        frontier = next_ids
    return collected


def _build_user_message(scope: SurveyorScope, context_body: str) -> str:
    scope_desc = (
        f"Project scope: {scope.project_id}"
        if isinstance(scope, ProjectScope)
        else f"Subgraph scope: roots={scope.root_ids}, depth={scope.depth}"
    )
    return (
        f"{scope_desc}\n\n"
        "Pages in scope (short-id, type, metadata, headline):\n"
        f"{context_body}\n\n"
        "Survey this scope. Return a summary plus any focus / reassess / "
        "consolidate / robustify recommendations. Use full page IDs as they "
        "appear above (the short 8-char prefix is displayed; recover the full "
        "ID from the context where needed — if you are not sure, omit the "
        "recommendation)."
    )


def _to_follow_ups(output: _SurveyScanOutput) -> list[FollowUp]:
    signals: list[FollowUp] = []
    for idea in output.focus:
        signals.append(
            FocusRequest(
                question_id=idea.question_id,
                priority=idea.priority,
                reason=idea.reason,
            )
        )
    for idea in output.reassess:
        signals.append(ReassessRequest(page_id=idea.page_id, reason=idea.reason))
    for idea in output.consolidate:
        if idea.page_ids:
            signals.append(ConsolidateRequest(page_ids=idea.page_ids, reason=idea.reason))
    for idea in output.robustify:
        signals.append(RobustifyRequest(claim_id=idea.claim_id, reason=idea.reason))
    return signals


class Surveyor:
    process_type = "surveyor"

    def __init__(
        self,
        db: DB,
        broadcaster: Broadcaster | None = None,
        context_char_budget: int = 8000,
        context_depth: int = 2,
    ):
        self.db = db
        self.broadcaster = broadcaster
        self.context_char_budget = context_char_budget
        self.context_depth = context_depth

    async def run(self, scope: SurveyorScope, budgets: BudgetEnvelope) -> Result:
        started = time.monotonic()
        status: str = "complete"
        self_report = ""
        signals: list[FollowUp] = []
        delta = MapDelta()

        root_ids = await _resolve_roots(self.db, scope)
        envelope_call = await self.db.create_call(
            CallType.PROCESS_ENVELOPE,
            scope_page_id=root_ids[0] if root_ids else None,
        )
        envelope_trace = CallTrace(
            call_id=envelope_call.id, db=self.db, broadcaster=self.broadcaster
        )
        await envelope_trace.record(
            ProcessStartedEvent(
                process_type=self.process_type,
                scope=scope.model_dump(),
                budgets=budgets.model_dump(),
            )
        )

        try:
            context_pages = await _gather_context_pages(self.db, root_ids, self.context_depth)
            context_body = _format_pages(context_pages, self.context_char_budget)

            user_message = _build_user_message(scope, context_body)
            scan_result = await structured_call(
                system_prompt=SYSTEM_PROMPT,
                user_message=user_message,
                response_model=_SurveyScanOutput,
                metadata=LLMExchangeMetadata(call_id=envelope_call.id, phase="survey_scan"),
                db=self.db,
            )
            output = scan_result.parsed
            if output is None:
                raise RuntimeError("survey_scan LLM returned no parseable output")

            delta = await self._commit_map_view(
                scope=scope,
                root_ids=root_ids,
                summary=output.summary,
                call_id=envelope_call.id,
            )
            signals = _to_follow_ups(output)

            self_report = f"surveyed {len(context_pages)} pages; emitted {len(signals)} signals"
        except Exception as exc:
            log.exception("Surveyor run failed: %s", exc)
            status = "failed"
            self_report = f"surveyor raised: {exc!r}"

        elapsed = time.monotonic() - started
        usage = ResourceUsage(
            compute=1 if status != "failed" else 0,
            ws_reads=len(delta.new_pages) + len(delta.new_links),
            writes=len(delta.new_pages) + len(delta.new_links),
            wallclock_seconds=elapsed,
        )

        result = Result(
            process_type=self.process_type,
            run_id=self.db.run_id,
            delta=delta,
            signals=signals,
            usage=usage,
            status=status,
            self_report=self_report,
        )
        await envelope_trace.record(
            ProcessCompletedEvent(
                process_type=self.process_type,
                status=result.status,
                self_report=result.self_report,
                delta=result.delta.model_dump(),
                signals=[sig.model_dump() for sig in result.signals],
                usage=result.usage.model_dump(),
            )
        )
        await self.db.update_call_status(
            envelope_call.id,
            CallStatus.COMPLETE,
            result_summary=self_report,
        )
        return result

    async def _commit_map_view(
        self,
        scope: SurveyorScope,
        root_ids: Sequence[str],
        summary: str,
        call_id: str,
    ) -> MapDelta:
        """Persist a View page holding the surveyor's synthesis."""
        scope_headline = (
            f"Survey of project {scope.project_id}"
            if isinstance(scope, ProjectScope)
            else f"Survey of subgraph ({len(scope.root_ids)} roots)"
        )
        view_page = Page(
            page_type=PageType.VIEW,
            layer=PageLayer.SQUIDGY,
            workspace=Workspace.RESEARCH,
            content=summary,
            headline=scope_headline,
            provenance_call_type=CallType.PROCESS_ENVELOPE.value,
            provenance_call_id=call_id,
        )
        await self.db.save_page(view_page)

        view_of_link: PageLink | None = None
        if root_ids:
            view_of_link = PageLink(
                from_page_id=view_page.id,
                to_page_id=root_ids[0],
                link_type=LinkType.VIEW_OF,
            )
            await self.db.save_link(view_of_link)

        new_pages = [
            PageRef(
                page_id=view_page.id,
                page_type=view_page.page_type,
                headline=view_page.headline,
            )
        ]
        new_links: list[LinkRef] = []
        if view_of_link is not None:
            new_links.append(
                LinkRef(
                    link_id=view_of_link.id,
                    from_page_id=view_of_link.from_page_id,
                    to_page_id=view_of_link.to_page_id,
                    link_type=view_of_link.link_type,
                )
            )
        return MapDelta(
            new_pages=new_pages,
            new_links=new_links,
            map_view_id=view_page.id,
        )
