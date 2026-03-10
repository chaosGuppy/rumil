"""Base types and shared helpers for moves."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

from differential.database import DB
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

S = TypeVar("S", bound=BaseModel)

PAGES_DIR = Path(__file__).parent.parent.parent.parent / "pages"


@dataclass
class MoveResult:
    """Result of executing a move."""
    message: str
    created_page_id: str | None = None


class MoveState:
    """Tracks what happened during a run_call."""

    def __init__(self, call: Call, db: DB):
        self.call = call
        self.db = db
        self.last_created_id: str | None = None
        self.created_page_ids: list[str] = []
        self.moves: list[Move] = []
        self.dispatches: list[Dispatch] = []


def _resolve_last_created(payload: BaseModel, last_created_id: str) -> BaseModel:
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
    execute: Callable[[S, Call, DB], MoveResult]

    def bind(self, state: MoveState) -> "Tool":
        """Return a Tool bound to this call's mutable state."""
        from differential.llm import Tool

        def fn(inp: dict) -> str:
            try:
                validated = self.schema(**inp)
            except Exception as e:
                print(f"  [validation] {self.name} failed: {e}")
                raise
            if state.last_created_id:
                validated = _resolve_last_created(validated, state.last_created_id)
            result = self.execute(validated, state.call, state.db)
            state.moves.append(Move(move_type=self.move_type, payload=validated))
            if result.created_page_id:
                state.created_page_ids.append(result.created_page_id)
                state.last_created_id = result.created_page_id
            return result.message

        return Tool(
            name=self.name,
            description=self.description,
            input_schema=self.schema.model_json_schema(),
            fn=fn,
        )


class CreatePagePayload(BaseModel):
    summary: str = Field(description=(
        "10-15 word headline summary (20 word ceiling). Must be a sharp, "
        "self-contained label — not a truncated sentence. Name the actual claim "
        "or position, e.g. 'Solar payback periods have fallen below 7 years in "
        "most climates'. Avoid vague openings like 'There are several factors...'."
    ))
    content: str = Field(description="Full explanation with reasoning. Be specific and substantive.")
    epistemic_status: float = Field(2.5, description="0-5 subjective confidence")
    epistemic_type: str = Field("", description="Nature of uncertainty, e.g. empirical, conceptual, contested")
    workspace: str = Field("research", description="research or prioritization")
    status: str | None = None
    remaining_fruit: float | None = None
    parent_question_id: str | None = None
    key_dependencies: str | None = Field(
        None, description="What this judgement most depends on (judgements only)"
    )
    sensitivity_analysis: str | None = Field(
        None, description="What would shift this judgement, and in which direction (judgements only)"
    )
    confidence_type: str | None = None
    decomposition_status: str | None = None
    source_url: str | None = None
    source_id: str | None = Field(None, description="Source page ID (ingest claims only)")
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


def create_page(
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

    db.save_page(page)
    write_page_file(page)
    print(f"  [+] {page_type.value}: {page.summary[:70]} [{page.id[:8]}]")

    message = (
        f"Created [{page.id[:8]}]: {payload.summary}"
        if payload.summary
        else f"Created [{page.id[:8]}]"
    )
    return MoveResult(message=message, created_page_id=page.id)


def link_pages(
    from_id: str,
    to_id: str,
    reasoning: str,
    db: DB,
    link_type: LinkType,
) -> MoveResult:
    """Create a link between two pages. Used by LINK_CHILD_QUESTION and LINK_RELATED."""
    from_id = db.resolve_page_id(from_id)
    to_id = db.resolve_page_id(to_id)
    if not from_id or not to_id:
        print(
            f"  [executor] {link_type.value} link skipped — "
            "one or both page IDs not found"
        )
        return MoveResult("Link skipped — page IDs not found.")

    link = PageLink(
        from_page_id=from_id,
        to_page_id=to_id,
        link_type=link_type,
        reasoning=reasoning,
    )
    db.save_link(link)
    print(
        f"  [~] {link_type.value}: {db.page_label(from_id)} -> {db.page_label(to_id)}"
    )
    return MoveResult("Done.")
