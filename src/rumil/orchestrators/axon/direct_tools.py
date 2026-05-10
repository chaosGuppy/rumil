"""Direct tools for axon — bounded I/O surfaces callable from mainline or delegates.

Slim by design. Mainline's only direct tool is ``load_page`` — read a
workspace page by its full ID. Multi-round work (web research,
workspace search, etc.) lives inside delegates: configure sets up the
right inner-loop system prompt + tools, and the inner loop does the
agentic work.

A delegate-callable ``create_page`` factory is in scope here so
configure can include it in a delegate's tool list when the delegate's
job is producing workspace pages. Mainline does NOT get create_page
directly — page creation is a delegate-internal concern; the page IDs
flow back via the finalize payload.

Context plumbing: tool fns need a :class:`rumil.database.DB`. The
orchestrator publishes a :class:`DirectToolCtx` via a contextvar
before each API call; tools read from it. Missing context raises
loudly — wiring bug, not silent fallback.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from rumil.context import format_page
from rumil.database import DB
from rumil.llm import Tool
from rumil.models import PageDetail
from rumil.orchestrators.axon.tools import register_direct_tool

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DirectToolCtx:
    """Per-run context shared with direct tool fns via a contextvar.

    Set by the orchestrator at run start (and not mutated thereafter
    for the lifetime of the run). Tool fns read it to find the active
    DB; missing-ctx access raises so wiring bugs surface.
    """

    db: DB
    call_id: str
    question_id: str | None = None  # active question for scoped operations


_DIRECT_TOOL_CTX: ContextVar[DirectToolCtx | None] = ContextVar("_DIRECT_TOOL_CTX", default=None)


def get_direct_tool_ctx() -> DirectToolCtx:
    ctx = _DIRECT_TOOL_CTX.get()
    if ctx is None:
        raise RuntimeError(
            "axon: direct tool fn invoked without DirectToolCtx — "
            "orchestrator must call set_direct_tool_ctx before mainline turns. Bug."
        )
    return ctx


def set_direct_tool_ctx(ctx: DirectToolCtx) -> object:
    """Set the contextvar; returns a token for ``ContextVar.reset``."""
    return _DIRECT_TOOL_CTX.set(ctx)


def reset_direct_tool_ctx(token: object) -> None:
    _DIRECT_TOOL_CTX.reset(token)  # pyright: ignore[reportArgumentType]


@contextmanager
def direct_tool_ctx_scope(ctx: DirectToolCtx) -> Iterator[None]:
    """Scope a DirectToolCtx for the duration of a ``with`` block."""
    token = set_direct_tool_ctx(ctx)
    try:
        yield
    finally:
        reset_direct_tool_ctx(token)


LOAD_PAGE_TOOL_NAME = "load_page"
CREATE_PAGE_TOOL_NAME = "create_page"


def build_load_page_tool() -> Tool:
    """Read a workspace page by ID; return its rendered content."""

    async def fn(args: dict) -> str:
        ctx = get_direct_tool_ctx()
        page_id = str(args.get("page_id", "")).strip()
        if not page_id:
            return "Error: load_page requires `page_id`."
        page = await ctx.db.get_page(page_id)
        if page is None:
            return f"Error: no page found with id {page_id!r}."
        return await format_page(page, detail=PageDetail.CONTENT, db=ctx.db)

    return Tool(
        name=LOAD_PAGE_TOOL_NAME,
        description=(
            "Load a workspace page's full content by its ID. Returns the "
            "page rendered with its type, headline, abstract, content, "
            "and key links (considerations, sub-questions, etc.). Use "
            "to read the body of any page you've seen referenced "
            "(seed pages at run start, page IDs returned by a delegate, "
            "ids cited in artifact text). The id is the full UUID, not "
            "the short 8-char form."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "page_id": {
                    "type": "string",
                    "description": "The full page id (UUID).",
                },
            },
            "required": ["page_id"],
            "additionalProperties": False,
        },
        fn=fn,
    )


def build_create_page_tool() -> Tool:
    """Create a workspace page; return its newly-assigned ID.

    Intended for inclusion in **delegate** tool lists, NOT mainline's.
    Mainline doesn't mutate the workspace directly — page creation
    happens inside delegates whose job is producing workspace
    artifacts.

    v1: stub — returns a not-implemented error. Real wiring requires
    the page-creation pipeline (type validation, link creation,
    embedding generation) and is a follow-up. Until then, delegates
    that need to surface results should use ``artifact_key`` to
    persist a summary string and let mainline read it back.
    """

    async def fn(args: dict) -> str:
        return (
            "Error: create_page is registered but not yet wired in v1. "
            "Use artifact_key on your delegate config to persist a "
            "summary string instead."
        )

    return Tool(
        name=CREATE_PAGE_TOOL_NAME,
        description=(
            "Create a workspace page (claim, source, view, etc.) and "
            "return its assigned id. For use by delegates whose job is "
            "producing durable workspace artifacts."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "page_type": {
                    "type": "string",
                    "enum": ["claim", "source", "view", "concept", "judgement"],
                    "description": "What kind of page to create.",
                },
                "headline": {
                    "type": "string",
                    "description": "Short title; one sentence max.",
                },
                "content": {
                    "type": "string",
                    "description": "Full body text.",
                },
            },
            "required": ["page_type", "headline", "content"],
            "additionalProperties": False,
        },
        fn=fn,
    )


register_direct_tool(LOAD_PAGE_TOOL_NAME, build_load_page_tool)
register_direct_tool(CREATE_PAGE_TOOL_NAME, build_create_page_tool)
