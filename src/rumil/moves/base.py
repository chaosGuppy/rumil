"""Base types and shared helpers for moves."""

import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

from rumil.database import DB
from rumil.embeddings import embed_and_store_page
from rumil.llm import LLMExchangeMetadata, Tool, structured_call
from rumil.models import (
    Call,
    Dispatch,
    LinkRole,
    LinkType,
    Move,
    MoveType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)
from rumil.settings import get_settings

DispatchValidator = Callable[[Dispatch], Dispatch | str]

log = logging.getLogger(__name__)

S = TypeVar("S", bound=BaseModel)
P = TypeVar("P", bound=BaseModel)


@dataclass
class MoveResult:
    """Result of executing a move."""

    message: str
    created_page_id: str | None = None
    dispatches: list[Dispatch] = field(default_factory=list)
    extra_created_ids: list[str] | None = None
    trace_extra: dict[str, Any] = field(default_factory=dict)


class MoveState:
    """Tracks what happened during a run_call."""

    def __init__(self, call: Call, db: DB):
        self.call = call
        self.db = db
        self.last_created_id: str | None = None
        self.created_page_ids: list[str] = []
        self.context_page_ids: set[str] = set()
        self.moves: list[Move] = []
        self.move_created_ids: list[list[str]] = []
        self.move_trace_extras: list[dict[str, Any]] = []
        self.dispatches: list[Dispatch] = []
        self._dispatch_validators: list[DispatchValidator] = []
        self._move_cursor: int = 0

    def record_dispatch(self, dispatch: Dispatch) -> str | None:
        """Validate and record a dispatch. Returns an error string if rejected."""
        for validator in self._dispatch_validators:
            result = validator(dispatch)
            if isinstance(result, str):
                return result
            dispatch = result
        self.dispatches.append(dispatch)
        return None

    def record_dispatches(self, dispatches: Sequence[Dispatch]) -> None:
        """Validate and record multiple dispatches, logging any rejections."""
        for d in dispatches:
            error = self.record_dispatch(d)
            if error:
                log.warning("Dispatch rejected: %s", error)

    def take_new_moves(
        self,
    ) -> tuple[list[Move], list[list[str]], list[dict[str, Any]]]:
        """Return moves, created-ID lists, and trace extras added since the last call."""
        new_moves = self.moves[self._move_cursor :]
        new_created = self.move_created_ids[self._move_cursor :]
        new_extras = self.move_trace_extras[self._move_cursor :]
        self._move_cursor = len(self.moves)
        return new_moves, new_created, new_extras


def _resolve_last_created(payload: P, last_created_id: str) -> P:
    """Replace any string field containing 'LAST_CREATED' with the actual ID."""
    updates = {}
    for field_name, value in payload:
        if value == "LAST_CREATED":
            updates[field_name] = last_created_id
    if updates:
        return payload.model_copy(update=updates)
    return payload


@dataclass
class MoveDef(Generic[S]):
    """Complete definition of a move: its identity, tool schema, and execution logic."""

    move_type: MoveType
    name: str
    description: str
    schema: type[S]
    execute: Callable[[S, Call, DB], Awaitable[MoveResult]]
    context_check: Callable[[S, "MoveState"], Awaitable[MoveResult | None]] | None = None

    def bind(self, state: MoveState) -> Tool:
        """Return a Tool bound to this call's mutable state."""

        async def fn(inp: dict) -> str:
            log.debug("Move %s called with input keys: %s", self.name, list(inp.keys()))
            try:
                validated = self.schema(**inp)
            except Exception as e:
                log.error("Move %s validation failed: %s", self.name, e, exc_info=True)
                raise
            if state.last_created_id:
                validated = _resolve_last_created(validated, state.last_created_id)
            if self.context_check:
                check_result = await self.context_check(validated, state)
                if check_result is not None:
                    return check_result.message
            result = await self.execute(validated, state.call, state.db)
            state.moves.append(Move(move_type=self.move_type, payload=validated))
            if result.dispatches:
                state.record_dispatches(result.dispatches)
            move_page_ids: list[str] = []
            if result.created_page_id:
                state.created_page_ids.append(result.created_page_id)
                state.last_created_id = result.created_page_id
                state.context_page_ids.add(result.created_page_id)
                move_page_ids.append(result.created_page_id)
                log.debug(
                    "Move %s created page: %s",
                    self.name,
                    result.created_page_id[:8],
                )
            if result.extra_created_ids:
                move_page_ids.extend(result.extra_created_ids)
            state.move_created_ids.append(move_page_ids)
            state.move_trace_extras.append(result.trace_extra)
            if self.move_type == MoveType.LOAD_PAGE:
                raw_pid = inp.get("page_id", "")
                if raw_pid:
                    loaded = await state.db.resolve_page_id(raw_pid)
                    if loaded:
                        state.context_page_ids.add(loaded)
            log.debug("Move %s result: %s", self.name, result.message[:100])
            return result.message

        return Tool(
            name=self.name,
            description=self.description,
            input_schema=self.schema.model_json_schema(),
            fn=fn,
        )


HEADLINE_DESCRIPTION = (
    "10-15 word headline (20 word ceiling). Must be a sharp, "
    "self-contained label — not a truncated sentence. Think of it like a newspaper "
    "headline: a reader with no prior context should know at a glance what this page "
    "is about. Name the actual claim or position, e.g. 'Solar payback periods have "
    "fallen below 7 years in most climates'. Never use language that only makes sense "
    "relative to a particular question or investigation — headlines are used for "
    "retrieval across the whole workspace and must stand alone. Always name the "
    "specific subject: 'The election is likely to take place' is broken because it "
    "doesn't say WHICH election. Avoid vague openings like 'There are several "
    "factors...' and context-dependent phrasing like 'This undercuts the premise', "
    "'Key factor in the timeline', or 'dominant cancellation pathway' (cancellation "
    "of what?)."
)


class CreatePagePayload(BaseModel):
    """Base payload shared by every create_* move.

    Carries only fields that make sense for every page type, including
    questions. Subclasses representing scored page types (claims, judgements,
    view items, wikis, summaries) should inherit from ScoredPagePayload to
    add the robustness fields.
    """

    headline: str = Field(description=HEADLINE_DESCRIPTION)
    content: str = Field(
        description="Full explanation with reasoning. Be specific and substantive."
    )
    workspace: str = Field("research", description="research or prioritization")
    supersedes: str | None = Field(
        None,
        description=(
            "Page ID of an existing page this one replaces. The old page is marked as superseded."
        ),
    )
    change_magnitude: int | None = Field(
        None,
        description=(
            "1-5: how much the picture changed from the superseded page. "
            "1=minor wording only, 3=substantive but same bottom line, "
            "5=completely changed the picture. Only used when supersedes is set."
        ),
    )

    def page_extra_fields(self) -> dict[str, Any]:
        """Return type-specific metadata fields to store in page.extra.

        Subclasses override to opt in their own fields. Structural fields
        like `links`, `supersedes`, etc. should NOT be included here — only
        metadata that should be persisted on the page itself.
        """
        return {}


class ScoredPagePayload(CreatePagePayload):
    """Base for page types that carry a robustness score: claims,
    judgements, view items, wikis, summaries. Questions do not inherit
    from this — they have no robustness.
    """

    robustness: int = Field(
        description=(
            "1-5 robustness scale. 1=wild guess, 3=considered view, "
            "5=highly robust. See preamble for full rubric."
        ),
    )
    robustness_reasoning: str = Field(
        description=(
            "Where the remaining uncertainty stems from and how reducible "
            "it is (e.g. 'would resolve with one clean benchmark run' vs "
            "'inherent — depends on future human behaviour')."
        ),
    )


def _resolve_workspace(ws: str) -> Workspace:
    return Workspace.RESEARCH if ws.lower() == "research" else Workspace.PRIORITIZATION


async def _copy_consideration_links(old_page_id: str, new_page_id: str, db: DB) -> None:
    """Copy outbound CONSIDERATION links from *old_page_id* to *new_page_id*.

    Skips links where *new_page_id* already has a CONSIDERATION link to the
    same target with the same direction.
    """
    old_links = await db.get_links_from(old_page_id)
    new_links = await db.get_links_from(new_page_id)
    existing = {
        (l.to_page_id, l.direction) for l in new_links if l.link_type == LinkType.CONSIDERATION
    }
    copied = 0
    for link in old_links:
        if link.link_type != LinkType.CONSIDERATION:
            continue
        if (link.to_page_id, link.direction) in existing:
            continue
        await db.save_link(
            PageLink(
                from_page_id=new_page_id,
                to_page_id=link.to_page_id,
                link_type=LinkType.CONSIDERATION,
                direction=link.direction,
                strength=link.strength,
                reasoning=link.reasoning,
                role=link.role,
            )
        )
        copied += 1
    if copied:
        log.info(
            "Copied %d consideration links from %s to %s",
            copied,
            old_page_id[:8],
            new_page_id[:8],
        )


async def create_page(
    payload: CreatePagePayload,
    call: Call,
    db: DB,
    page_type: PageType,
    layer: PageLayer,
    credence: int | None = None,
    credence_reasoning: str | None = None,
    robustness: int | None = None,
    robustness_reasoning: str | None = None,
) -> MoveResult:
    """Create a page from payload, save to DB and file system.

    `credence` is only meaningful for CLAIM pages. `robustness` is meaningful
    for every scored page type (claim, judgement, view item, wiki, summary);
    questions leave both at None.
    """
    workspace = _resolve_workspace(payload.workspace)
    extra = payload.page_extra_fields()

    fruit_remaining = getattr(payload, "fruit_remaining", None)
    is_claim = page_type == PageType.CLAIM
    page = Page(
        page_type=page_type,
        layer=layer,
        workspace=workspace,
        content=payload.content,
        headline=payload.headline,
        credence=credence if is_claim else None,
        credence_reasoning=credence_reasoning if is_claim else None,
        robustness=robustness,
        robustness_reasoning=robustness_reasoning,
        fruit_remaining=fruit_remaining,
        provenance_model=get_settings().model,
        provenance_call_type=call.call_type.value,
        provenance_call_id=call.id,
        extra=extra,
    )

    await db.save_page(page)
    try:
        await embed_and_store_page(db, page, field_name="abstract")
    except Exception:
        log.warning("Failed to create embedding for page %s", page.id[:8], exc_info=True)
    log.info(
        "Page created: type=%s, id=%s, headline=%s",
        page_type.value,
        page.id[:8],
        page.headline[:70],
    )

    try:
        cited_ids = await extract_and_link_citations(
            page.id,
            page.content,
            db,
            call=call,
        )
        if cited_ids:
            log.info(
                "Auto-linked %d citations from page %s",
                len(cited_ids),
                page.id[:8],
            )
    except Exception:
        log.warning(
            "Citation extraction failed for page %s",
            page.id[:8],
            exc_info=True,
        )

    if payload.supersedes:
        old_id = await db.resolve_page_id(payload.supersedes)
        if old_id:
            await db.supersede_page(
                old_id,
                page.id,
                change_magnitude=payload.change_magnitude,
            )
            await _copy_consideration_links(old_id, page.id, db)
            log.info("Superseded %s -> %s", old_id[:8], page.id[:8])
        else:
            log.warning("Supersede target %s not found", payload.supersedes)

    message = (
        f"Created [{page.id[:8]}]: {payload.headline}"
        if payload.headline
        else f"Created [{page.id[:8]}]"
    )
    return MoveResult(message=message, created_page_id=page.id)


_CITATION_RE = re.compile(r"\[([a-f0-9]{8})\]")

_STRENGTH_SYSTEM_PROMPT = (
    "You are assigning dependency strengths for a newly-created claim or "
    "judgement. The page's content is its derivation — the argument for "
    "the page, citing each direct dependency inline with a short ID in "
    "square brackets.\n\n"
    "For each cited page listed in the user message, return an entry with "
    "its 8-character page_id, a strength 1-5, and a one-sentence reasoning "
    "naming the role the cited page plays in the derivation.\n"
    "- 1 = weak; if the cited page were false, the citing page would mostly stand.\n"
    "- 3 = material; the argument would need reworking.\n"
    "- 5 = fully load-bearing; the citing page would collapse.\n\n"
    "Return one entry per cited page; do not invent IDs."
)


class _CitedPageStrength(BaseModel):
    page_id: str = Field(description="8-character short ID of the cited page")
    strength: float = Field(
        description=(
            "1-5: how load-bearing this dependency is for the citing page "
            "(1 = mildly depends on, 5 = would collapse without it)"
        ),
    )
    reasoning: str = Field(
        "",
        description="One sentence: what role the cited page plays in the derivation",
    )


class _DependencyStrengths(BaseModel):
    strengths: list[_CitedPageStrength]


_STRENGTH_ELIGIBLE_CITING_TYPES: frozenset[PageType] = frozenset(
    {PageType.CLAIM, PageType.JUDGEMENT}
)


def _citation_excerpts(content: str, short_id: str, max_lines: int = 3) -> Sequence[str]:
    """Return up to *max_lines* lines from *content* that cite *short_id*."""
    pattern = re.compile(rf"[^\n]*\[{re.escape(short_id)}\][^\n]*")
    return pattern.findall(content)[:max_lines]


async def _assign_dependency_strengths(
    citing_page: Page,
    cited_pages: Sequence[Page],
    call: Call | None,
    db: DB,
) -> dict[str, tuple[float, str]]:
    """Ask Sonnet to assign dependency strengths for each cited page.

    Returns a mapping ``{cited_page_id: (strength, reasoning)}``. On LLM
    failure or if a cited page is missing from the response, that page's
    entry defaults to ``(2.5, "")``.
    """
    if not cited_pages:
        return {}

    defaults: dict[str, tuple[float, str]] = {p.id: (2.5, "") for p in cited_pages}

    entries: list[str] = []
    for p in cited_pages:
        excerpts = _citation_excerpts(citing_page.content, p.id[:8])
        cite_block = (
            "\n".join(f"> {line.strip()}" for line in excerpts)
            if excerpts
            else "> (citation not found in content)"
        )
        entries.append(
            f"### [{p.id[:8]}] {p.headline} ({p.page_type.value})\n"
            f"Cited in the new page as:\n{cite_block}\n"
        )

    citing_type_label = citing_page.page_type.value
    user_message = (
        f"## New {citing_type_label}\n**{citing_page.headline}**\n\n"
        f"{citing_page.content}\n\n"
        "---\n\n"
        f"## Cited pages ({len(cited_pages)})\n\n" + "\n".join(entries)
    )

    settings = get_settings()
    metadata: LLMExchangeMetadata | None = None
    db_arg: DB | None = None
    if call is not None:
        metadata = LLMExchangeMetadata(
            call_id=call.id,
            phase="assign_dependency_strengths",
        )
        db_arg = db

    try:
        result = await structured_call(
            system_prompt=_STRENGTH_SYSTEM_PROMPT,
            user_message=user_message,
            response_model=_DependencyStrengths,
            metadata=metadata,
            db=db_arg,
            model=settings.sonnet_model,
        )
    except Exception:
        log.warning(
            "Dependency strength call failed for page %s; using defaults",
            citing_page.id[:8],
            exc_info=True,
        )
        return defaults

    if result.parsed is None:
        log.warning(
            "Dependency strength call returned no parsed output for page %s",
            citing_page.id[:8],
        )
        return defaults

    short_to_full = {p.id[:8]: p.id for p in cited_pages}
    full_set = {p.id for p in cited_pages}
    assigned: dict[str, tuple[float, str]] = dict(defaults)
    for entry in result.parsed.strengths:
        pid = entry.page_id
        full = short_to_full.get(pid) if len(pid) == 8 else (pid if pid in full_set else None)
        if full is None:
            log.debug("Strength entry for unknown page_id %s ignored", pid)
            continue
        strength = max(1.0, min(5.0, float(entry.strength)))
        assigned[full] = (strength, entry.reasoning)
    return assigned


async def extract_and_link_citations(
    page_id: str,
    content: str,
    db: DB,
    call: Call | None = None,
) -> set[str]:
    """Extract [shortid] citations from content and create page links.

    Questions are never valid citation targets — when a citation resolves
    to a question, it is rewritten to that question's current judgement.
    If the question has no judgement yet, the citation is skipped with a
    warning. After this rewrite, link type depends on the citing and
    cited pages' types:

    - Cited SOURCE → CITES (from=citing, to=cited)
    - Citing CLAIM/JUDGEMENT cites a CLAIM/JUDGEMENT → DEPENDS_ON
      (from=citing, to=cited): the citing page's conclusions rest on the
      cited page being true.
    - Citing QUESTION cites a CLAIM/JUDGEMENT → RELATED
      (from=cited, to=citing): inline citations from a question's body are
      not strong enough to count as considerations bearing on the question —
      they are just a general relation.
    - Otherwise → RELATED (from=cited, to=citing).

    Returns the set of full UUIDs that were successfully linked.
    """
    matches = set(_CITATION_RE.findall(content))
    own_short_id = page_id[:8]
    matches.discard(own_short_id)
    if not matches:
        return set()

    resolved_map = await db.resolve_page_ids(list(matches))
    needed_ids = list({pid for pid in resolved_map.values()} | {page_id})
    pages = await db.get_pages_by_ids(needed_ids)
    citing_page = pages.get(page_id)
    citing_type = citing_page.page_type if citing_page else None

    pending: list[tuple[str, str, LinkType, Page]] = []
    for short_id in matches:
        resolved = resolved_map.get(short_id)
        if not resolved:
            log.debug("Citation [%s] did not resolve to a page", short_id)
            continue

        cited_page = pages.get(resolved)
        if not cited_page:
            continue

        if cited_page.page_type == PageType.QUESTION:
            judgements = await db.get_judgements_for_question(cited_page.id)
            if not judgements:
                log.warning(
                    "Citation [%s] points at a question with no judgement; "
                    "skipping. Cite the question's judgement, not the question.",
                    short_id,
                )
                continue
            cited_page = judgements[0]
            resolved = cited_page.id
            if resolved == page_id:
                continue

        if cited_page.page_type == PageType.SOURCE:
            link_type = LinkType.CITES
            from_id, to_id = page_id, resolved
        elif cited_page.page_type in (PageType.CLAIM, PageType.JUDGEMENT):
            if citing_type in (PageType.CLAIM, PageType.JUDGEMENT, PageType.VIEW_ITEM):
                link_type = LinkType.DEPENDS_ON
                from_id, to_id = page_id, resolved
            else:
                link_type = LinkType.RELATED
                from_id, to_id = resolved, page_id
        else:
            link_type = LinkType.RELATED
            from_id, to_id = resolved, page_id

        pending.append((from_id, to_id, link_type, cited_page))

    strengths: dict[str, tuple[float, str]] = {}
    if citing_type in _STRENGTH_ELIGIBLE_CITING_TYPES and citing_page is not None:
        depends_on_targets = [cited for (_, _, lt, cited) in pending if lt == LinkType.DEPENDS_ON]
        if depends_on_targets:
            strengths = await _assign_dependency_strengths(
                citing_page,
                depends_on_targets,
                call,
                db,
            )

    linked: set[str] = set()
    for from_id, to_id, link_type, cited_page in pending:
        if link_type == LinkType.DEPENDS_ON and cited_page.id in strengths:
            strength, reasoning = strengths[cited_page.id]
            link = PageLink(
                from_page_id=from_id,
                to_page_id=to_id,
                link_type=link_type,
                strength=strength,
                reasoning=reasoning,
            )
        else:
            link = PageLink(
                from_page_id=from_id,
                to_page_id=to_id,
                link_type=link_type,
            )
        await db.save_link(link)
        if link_type == LinkType.DEPENDS_ON:
            log.info(
                "Citation linked: %s -> %s (%s, %.1f)",
                from_id[:8],
                to_id[:8],
                link_type.value,
                link.strength,
            )
        else:
            log.info(
                "Citation linked: %s -> %s (%s)",
                from_id[:8],
                to_id[:8],
                link_type.value,
            )
        linked.add(cited_page.id)

    return linked


async def link_pages(
    from_id: str,
    to_id: str,
    reasoning: str,
    db: DB,
    link_type: LinkType,
    role: LinkRole = LinkRole.DIRECT,
    impact_on_parent_question: int | None = None,
) -> MoveResult:
    """Create a link between two pages. Used by LINK_CHILD_QUESTION and LINK_RELATED."""
    resolved_from = await db.resolve_page_id(from_id)
    resolved_to = await db.resolve_page_id(to_id)
    if not resolved_from or not resolved_to:
        log.warning(
            "Link %s skipped: from_id=%s, to_id=%s — one or both not found",
            link_type.value,
            resolved_from,
            resolved_to,
        )
        return MoveResult("Link skipped — page IDs not found.")

    link = PageLink(
        from_page_id=resolved_from,
        to_page_id=resolved_to,
        link_type=link_type,
        reasoning=reasoning,
        role=role,
        impact_on_parent_question=impact_on_parent_question,
    )
    await db.save_link(link)
    log.info(
        "Link created: %s %s -> %s",
        link_type.value,
        resolved_from[:8],
        resolved_to[:8],
    )
    return MoveResult("Done.")


async def supersede_old_judgements(
    new_judgement_id: str,
    question_id: str,
    db: DB,
    change_magnitude: int | None = None,
) -> None:
    """Supersede any existing active judgements on a question when a new one is linked."""
    old_judgements = await db.get_judgements_for_question(question_id)
    for old in old_judgements:
        if old.id == new_judgement_id:
            continue
        await db.supersede_page(
            old.id,
            new_judgement_id,
            change_magnitude=change_magnitude,
        )
        log.info(
            "Superseded old judgement %s with %s on question %s",
            old.id[:8],
            new_judgement_id[:8],
            question_id[:8],
        )
