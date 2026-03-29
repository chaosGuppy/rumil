"""Base types and shared helpers for moves."""

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Generic, TypeVar
import re

from pydantic import BaseModel, Field

from rumil.database import DB
from rumil.embeddings import embed_and_store_page
from rumil.llm import Tool
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

DispatchValidator = Callable[[Dispatch], Dispatch | str]

log = logging.getLogger(__name__)

S = TypeVar("S", bound=BaseModel)
P = TypeVar("P", bound=BaseModel)

PAGES_DIR = Path(__file__).parent.parent.parent.parent / "pages"


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
            result = await self.execute(validated, state.call, state.db)
            state.moves.append(Move(move_type=self.move_type, payload=validated))
            if result.dispatches:
                state.record_dispatches(result.dispatches)
            move_page_ids: list[str] = []
            if result.created_page_id:
                state.created_page_ids.append(result.created_page_id)
                state.last_created_id = result.created_page_id
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
            log.debug("Move %s result: %s", self.name, result.message[:100])
            return result.message

        return Tool(
            name=self.name,
            description=self.description,
            input_schema=self.schema.model_json_schema(),
            fn=fn,
        )


class CreatePagePayload(BaseModel):
    headline: str = Field(
        description=(
            "10-15 word headline (20 word ceiling). Must be a sharp, "
            "self-contained label — not a truncated sentence. Name the actual claim "
            "or position, e.g. 'Solar payback periods have fallen below 7 years in "
            "most climates'. Avoid vague openings like 'There are several factors...'."
        )
    )
    content: str = Field(
        description="Full explanation with reasoning. Be specific and substantive."
    )
    credence: int = Field(
        5,
        description=(
            "1-9 credence scale. 1=virtually impossible, 5=genuinely uncertain, "
            "9=completely uncontroversial. See preamble for full rubric."
        ),
    )
    robustness: int = Field(
        1,
        description=(
            "1-5 robustness scale. 1=wild guess, 3=considered view, "
            "5=highly robust. See preamble for full rubric."
        ),
    )
    workspace: str = Field("research", description="research or prioritization")
    supersedes: str | None = Field(
        None,
        description=(
            "Page ID of an existing page this one replaces. The old page "
            "is marked as superseded."
        ),
    )

    def page_extra_fields(self) -> dict[str, Any]:
        """Return type-specific metadata fields to store in page.extra.

        Subclasses override to opt in their own fields. Structural fields
        like `links`, `supersedes`, etc. should NOT be included here — only
        metadata that should be persisted on the page itself.
        """
        return {}


def _resolve_workspace(ws: str) -> Workspace:
    return Workspace.RESEARCH if ws.lower() == "research" else Workspace.PRIORITIZATION


def _pages_dir(workspace: Workspace) -> Path:
    d = PAGES_DIR / workspace.value
    d.mkdir(parents=True, exist_ok=True)
    return d


def _page_filename(page: Page) -> str:
    slug = page.headline[:60].lower()
    slug = "".join(c if c.isalnum() or c in " -" else "" for c in slug)
    slug = slug.strip().replace(" ", "-")
    short_id = page.id[:8]
    return f"{page.page_type.value}-{short_id}-{slug}.md"


def write_page_file(page: Page) -> None:
    """Write a human-readable markdown file for a page."""
    d = _pages_dir(page.workspace)
    filepath = d / _page_filename(page)

    extra = page.extra or {}

    lines = [
        f"# {page.headline}",
        "",
        f"**Type:** {page.page_type.value}  ",
        f"**Layer:** {page.layer.value}  ",
        f"**ID:** `{page.id}`  ",
        f"**Created:** {page.created_at.strftime('%Y-%m-%d %H:%M:%S')} UTC  ",
        f"**Provenance:** {page.provenance_call_type} call `{page.provenance_call_id[:8]}`  ",
    ]
    if page.credence is not None:
        lines.insert(-1, f"**Credence:** {page.credence}/9 | **Robustness:** {page.robustness}/5  ")

    if page.is_superseded:
        lines.append(f"**SUPERSEDED by:** `{page.superseded_by}`  ")

    if extra:
        lines.append("")
        lines.append("## Metadata")
        for k, v in extra.items():
            lines.append(f"- **{k}:** {v}")

    lines += ["", "---", "", page.content]

    filepath.write_text("\n".join(lines), encoding="utf-8")


async def create_page(
    payload: CreatePagePayload,
    call: Call,
    db: DB,
    page_type: PageType,
    layer: PageLayer,
) -> MoveResult:
    """Create a page from payload, save to DB and file system."""
    workspace = _resolve_workspace(payload.workspace)
    extra = payload.page_extra_fields()

    page = Page(
        page_type=page_type,
        layer=layer,
        workspace=workspace,
        content=payload.content,
        headline=payload.headline,
        credence=None if page_type == PageType.QUESTION else payload.credence,
        robustness=None if page_type == PageType.QUESTION else payload.robustness,
        provenance_model="claude-opus-4-6",
        provenance_call_type=call.call_type.value,
        provenance_call_id=call.id,
        extra=extra,
    )

    await db.save_page(page)
    write_page_file(page)
    try:
        await embed_and_store_page(db, page, field_name="abstract")
    except Exception:
        log.warning(
            "Failed to create embedding for page %s", page.id[:8], exc_info=True
        )
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
            citing_page_type=page_type,
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
            await db.supersede_page(old_id, page.id)
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


async def extract_and_link_citations(
    page_id: str,
    content: str,
    db: DB,
    citing_page_type: PageType | None = None,
) -> set[str]:
    """Extract [shortid] citations from content and create page links.

    Link type depends on both citing and cited page types:
    - Cited SOURCE → CITES
    - Cited CLAIM (from QUESTION/JUDGEMENT/CLAIM) → CONSIDERATION (direction flipped:
      from=cited claim, to=citing page, since "claim bears on citing page")
    - Otherwise → RELATED

    Returns the set of full UUIDs that were successfully linked.
    """
    matches = set(_CITATION_RE.findall(content))
    own_short_id = page_id[:8]
    matches.discard(own_short_id)
    if not matches:
        return set()

    linked: set[str] = set()
    for short_id in matches:
        resolved = await db.resolve_page_id(short_id)
        if not resolved:
            log.debug("Citation [%s] did not resolve to a page", short_id)
            continue

        cited_page = await db.get_page(resolved)
        if not cited_page:
            continue

        from_id, to_id = page_id, resolved
        if cited_page.page_type == PageType.SOURCE:
            link_type = LinkType.CITES
        elif cited_page.page_type == PageType.CLAIM and citing_page_type in (
            PageType.QUESTION,
            PageType.JUDGEMENT,
            PageType.CLAIM,
        ):
            link_type = LinkType.CONSIDERATION
            from_id, to_id = resolved, page_id
        else:
            link_type = LinkType.RELATED

        await db.save_link(
            PageLink(
                from_page_id=from_id,
                to_page_id=to_id,
                link_type=link_type,
            )
        )
        log.info(
            "Citation linked: %s -> %s (%s)",
            from_id[:8],
            to_id[:8],
            link_type.value,
        )
        linked.add(resolved)

    return linked


async def link_pages(
    from_id: str,
    to_id: str,
    reasoning: str,
    db: DB,
    link_type: LinkType,
    role: LinkRole = LinkRole.DIRECT,
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
) -> None:
    """Supersede any existing active judgements on a question when a new one is linked."""
    old_judgements = await db.get_judgements_for_question(question_id)
    for old in old_judgements:
        if old.id == new_judgement_id:
            continue
        await db.supersede_page(old.id, new_judgement_id)
        log.info(
            "Superseded old judgement %s with %s on question %s",
            old.id[:8],
            new_judgement_id[:8],
            question_id[:8],
        )
