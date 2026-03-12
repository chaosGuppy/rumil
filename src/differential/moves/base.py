"""Base types and shared helpers for moves."""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

from differential.database import DB
from differential.llm import Tool
from differential.models import (
    Call,
    Dispatch,
    LinkType,
    Move,
    MoveType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)

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


class MoveState:
    """Tracks what happened during a run_call."""

    def __init__(self, call: Call, db: DB):
        self.call = call
        self.db = db
        self.last_created_id: str | None = None
        self.created_page_ids: list[str] = []
        self.moves: list[Move] = []
        self.move_created_ids: list[list[str]] = []
        self.dispatches: list[Dispatch] = []


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
                state.dispatches.extend(result.dispatches)
            move_page_ids: list[str] = []
            if result.created_page_id:
                state.created_page_ids.append(result.created_page_id)
                state.last_created_id = result.created_page_id
                move_page_ids.append(result.created_page_id)
                log.debug(
                    "Move %s created page: %s",
                    self.name, result.created_page_id[:8],
                )
            if result.extra_created_ids:
                move_page_ids.extend(result.extra_created_ids)
            state.move_created_ids.append(move_page_ids)
            log.debug("Move %s result: %s", self.name, result.message[:100])
            return result.message

        return Tool(
            name=self.name,
            description=self.description,
            input_schema=self.schema.model_json_schema(),
            fn=fn,
        )


class CreatePagePayload(BaseModel):
    summary: str = Field(
        description=(
            "10-15 word headline summary (20 word ceiling). Must be a sharp, "
            "self-contained label — not a truncated sentence. Name the actual claim "
            "or position, e.g. 'Solar payback periods have fallen below 7 years in "
            "most climates'. Avoid vague openings like 'There are several factors...'."
        )
    )
    content: str = Field(
        description="Full explanation with reasoning. Be specific and substantive."
    )
    epistemic_status: float = Field(2.5, description="0-5 subjective confidence")
    epistemic_type: str = Field(
        "", description="Nature of uncertainty, e.g. empirical, conceptual, contested"
    )
    workspace: str = Field("research", description="research or prioritization")
    status: str | None = None
    remaining_fruit: float | None = None
    parent_question_id: str | None = None
    key_dependencies: str | None = Field(
        None, description="What this judgement most depends on (judgements only)"
    )
    sensitivity_analysis: str | None = Field(
        None,
        description="What would shift this judgement, and in which direction (judgements only)",
    )
    confidence_type: str | None = None
    decomposition_status: str | None = None
    source_url: str | None = None
    source_id: str | None = Field(
        None, description="Source page ID (ingest claims only)"
    )
    direction: str | None = None
    strength: float | None = None
    hypothesis: str | None = None


def _resolve_workspace(ws: str) -> Workspace:
    return Workspace.RESEARCH if ws.lower() == "research" else Workspace.PRIORITIZATION


def _pages_dir(workspace: Workspace) -> Path:
    d = PAGES_DIR / workspace.value
    d.mkdir(parents=True, exist_ok=True)
    return d


def _page_filename(page: Page) -> str:
    slug = page.summary[:60].lower()
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
        f"# {page.summary}",
        "",
        f"**Type:** {page.page_type.value}  ",
        f"**Layer:** {page.layer.value}  ",
        f"**ID:** `{page.id}`  ",
        f"**Created:** {page.created_at.strftime('%Y-%m-%d %H:%M:%S')} UTC  ",
        f"**Epistemic status:** {page.epistemic_status:.2f} — {page.epistemic_type}  ",
        f"**Provenance:** {page.provenance_call_type} call `{page.provenance_call_id[:8]}`  ",
    ]

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
    extra: dict[str, Any] = {}

    for key in [
        "status",
        "remaining_fruit",
        "parent_question_id",
        "key_dependencies",
        "sensitivity_analysis",
        "confidence_type",
        "decomposition_status",
        "source_url",
        "source_id",
        "direction",
        "strength",
        "hypothesis",
    ]:
        val = getattr(payload, key, None)
        if val is not None:
            extra[key] = val

    page = Page(
        page_type=page_type,
        layer=layer,
        workspace=workspace,
        content=payload.content,
        summary=payload.summary,
        epistemic_status=payload.epistemic_status,
        epistemic_type=payload.epistemic_type,
        provenance_model="claude-opus-4-6",
        provenance_call_type=call.call_type.value,
        provenance_call_id=call.id,
        extra=extra,
    )

    await db.save_page(page)
    write_page_file(page)
    log.info(
        "Page created: type=%s, id=%s, summary=%s",
        page_type.value, page.id[:8], page.summary[:70],
    )

    message = (
        f"Created [{page.id[:8]}]: {payload.summary}"
        if payload.summary
        else f"Created [{page.id[:8]}]"
    )
    return MoveResult(message=message, created_page_id=page.id)


async def link_pages(
    from_id: str,
    to_id: str,
    reasoning: str,
    db: DB,
    link_type: LinkType,
) -> MoveResult:
    """Create a link between two pages. Used by LINK_CHILD_QUESTION and LINK_RELATED."""
    resolved_from = await db.resolve_page_id(from_id)
    resolved_to = await db.resolve_page_id(to_id)
    if not resolved_from or not resolved_to:
        log.warning(
            "Link %s skipped: from_id=%s, to_id=%s — one or both not found",
            link_type.value, resolved_from, resolved_to,
        )
        return MoveResult("Link skipped — page IDs not found.")

    link = PageLink(
        from_page_id=resolved_from,
        to_page_id=resolved_to,
        link_type=link_type,
        reasoning=reasoning,
    )
    await db.save_link(link)
    log.info(
        "Link created: %s %s -> %s",
        link_type.value, resolved_from[:8], resolved_to[:8],
    )
    return MoveResult("Done.")
