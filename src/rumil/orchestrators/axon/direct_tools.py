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
from rumil.models import Page, PageDetail, PageLayer, PageType, Workspace
from rumil.orchestrators.axon.artifacts import ArtifactStore
from rumil.orchestrators.axon.tools import register_direct_tool

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DirectToolCtx:
    """Per-run context shared with direct tool fns via a contextvar.

    Set by the orchestrator at run start (and not mutated thereafter
    for the lifetime of the run). Tool fns read it to find the active
    DB / ArtifactStore; missing-ctx access raises so wiring bugs surface.
    """

    db: DB
    call_id: str
    question_id: str | None = None  # active question for scoped operations
    artifacts: ArtifactStore | None = None


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
READ_ARTIFACT_TOOL_NAME = "read_artifact"
RECORD_OPERATOR_FEEDBACK_TOOL_NAME = "record_operator_feedback"


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


_CREATE_PAGE_ALLOWED_TYPES = ("claim", "source", "view", "judgement")


def build_create_page_tool() -> Tool:
    """Create a workspace page; return its newly-assigned ID.

    Intended for inclusion in **delegate** tool lists, NOT mainline's.
    Mainline doesn't mutate the workspace directly — page creation
    happens inside delegates whose job is producing workspace
    artifacts (research syntheses, source pages, claims, judgements).

    Allowed page types are restricted to ``claim``, ``source``,
    ``view``, ``judgement`` — the durable, content-bearing kinds.
    Question pages have scoping constraints (parent question linkage)
    that need a more deliberate API; wiki / summary / view_item /
    view_meta / spec_item / artefact are managed by other paths and
    not safe for ad-hoc delegate creation.

    The page is persisted via :meth:`DB.save_page` with run_id /
    project_id taken from the orchestrator's DB context. Returns the
    new page id as the tool's string result so the delegate's finalize
    payload can surface it.
    """

    async def fn(args: dict) -> str:
        ctx = get_direct_tool_ctx()
        page_type_raw = str(args.get("page_type", "")).strip()
        headline = str(args.get("headline", "")).strip()
        content = str(args.get("content", "")).strip()
        if not page_type_raw:
            return "Error: create_page requires `page_type`."
        if not headline:
            return "Error: create_page requires `headline`."
        if not content:
            return "Error: create_page requires `content`."
        if page_type_raw not in _CREATE_PAGE_ALLOWED_TYPES:
            return (
                f"Error: page_type {page_type_raw!r} is not allowed via "
                f"create_page (allowed: {list(_CREATE_PAGE_ALLOWED_TYPES)})."
            )
        try:
            page_type = PageType(page_type_raw)
        except ValueError as e:
            return f"Error: invalid page_type: {e}"
        page = Page(
            page_type=page_type,
            layer=PageLayer.SQUIDGY,
            workspace=Workspace.RESEARCH,
            content=content,
            headline=headline,
            project_id=ctx.db.project_id or "",
            provenance_call_id=ctx.call_id or "",
            run_id=ctx.db.run_id or "",
            scope_question_id=ctx.question_id,
        )
        try:
            await ctx.db.save_page(page)
        except Exception as e:
            log.exception("axon.create_page: save_page failed")
            return f"Error: failed to save page: {type(e).__name__}: {e}"
        return f"Created page id={page.id} (type={page_type.value}, headline={headline!r})."

    return Tool(
        name=CREATE_PAGE_TOOL_NAME,
        description=(
            "Create a workspace page (one of claim, source, view, "
            "judgement) and return its assigned id. For use by "
            "delegates whose job is producing durable workspace "
            "artifacts (research syntheses, scraped sources, claims, "
            "judgements). The page is persisted to the active "
            "project / run; the assigned id can be surfaced in your "
            "finalize payload so your caller can load_page it later. "
            "Mainline does not have this tool — only delegates with "
            "create_page in their configured tools list."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "page_type": {
                    "type": "string",
                    "enum": list(_CREATE_PAGE_ALLOWED_TYPES),
                    "description": (
                        "claim / source / view / judgement. "
                        "Question pages need parent linkage and are "
                        "not creatable via this tool."
                    ),
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


def build_read_artifact_tool() -> Tool:
    """Read an artifact's body by key from the run's ArtifactStore.

    Caller-seeded artifacts (with ``render_inline=False``, the default)
    are announced in the spine's first user message but their bodies
    are not spliced inline; this tool fetches the body on demand.
    Same goes for delegate-produced artifacts that mainline wants to
    inspect after a tool_result mentions the key.
    """

    async def fn(args: dict) -> str:
        ctx = get_direct_tool_ctx()
        if ctx.artifacts is None:
            return "Error: read_artifact requires the orchestrator to have wired an ArtifactStore into the contextvar; none is set."
        key = str(args.get("key", "")).strip()
        if not key:
            return "Error: read_artifact requires `key`."
        artifact = ctx.artifacts.get(key)
        if artifact is None:
            available = ctx.artifacts.list_keys()
            return f"Error: no artifact at key {key!r}. Available keys: {available}"
        provenance = (
            "input" if artifact.produced_by == "input" else f"delegate:{artifact.produced_by}"
        )
        desc_part = f" — {artifact.description}" if artifact.description else ""
        header = (
            f'<artifact key="{artifact.key}" chars="{len(artifact.text)}" '
            f'from="{provenance}"{desc_part}>'
        )
        return f"{header}\n{artifact.text}\n</artifact>"

    return Tool(
        name=READ_ARTIFACT_TOOL_NAME,
        description=(
            "Read the body of a run-local artifact by its key. "
            "Artifacts are announced in your initial user message under "
            "'## Available artifacts' and as tool_result text when "
            "delegates write artifacts via their write_artifact side "
            "effect. Use this to load the full body of any artifact "
            "whose key has been announced — caller-seeded inputs "
            "(prefixes, rubrics, prior syntheses) and delegate "
            "outputs alike. The reserved `operating_assumptions` key "
            "is also readable here. Returns the body wrapped in an "
            "<artifact key=...> XML fence with chars + provenance."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "The artifact key to load.",
                },
            },
            "required": ["key"],
            "additionalProperties": False,
        },
        fn=fn,
    )


def build_record_operator_feedback_tool() -> Tool:
    """Record a structured comment for the operator.

    Records an :class:`AxonOperatorFeedbackEvent` on the active trace
    so a human reviewing the run can see orchestrator-level friction
    the model noticed. Whether the event came from mainline or a
    specific delegate is inferable from temporal ordering against the
    surrounding delegate-bracketing trace events.
    """

    async def fn(args: dict) -> str:
        from rumil.orchestrators.axon.trace_events import AxonOperatorFeedbackEvent
        from rumil.tracing.tracer import get_trace

        # Touch the contextvar so wiring bugs (no ctx set) surface the
        # same way they do for load_page / read_artifact.
        get_direct_tool_ctx()
        subject = str(args.get("subject", "")).strip()
        detail = str(args.get("detail", "")).strip()
        if not subject:
            return "Error: record_operator_feedback requires `subject`."
        if not detail:
            return "Error: record_operator_feedback requires `detail`."
        suggestion = str(args.get("suggestion", "") or "").strip()
        severity = str(args.get("severity", "info")).strip() or "info"
        if severity not in ("info", "warn", "blocker"):
            return f"Error: severity {severity!r} not in (info, warn, blocker)."
        trace = get_trace()
        if trace is None:
            log.warning("record_operator_feedback: no active trace; dropping event")
            return "Error: no active trace; feedback not recorded."
        await trace.record(
            AxonOperatorFeedbackEvent(
                subject=subject,
                detail=detail,
                suggestion=suggestion,
                severity=severity,  # pyright: ignore[reportArgumentType]
            )
        )
        return f"Recorded operator feedback ({severity}): {subject}"

    return Tool(
        name=RECORD_OPERATOR_FEEDBACK_TOOL_NAME,
        description=(
            "Record a short structured comment for the operator about "
            "orchestrator-level friction — bad prompts, missing or "
            "mis-named tools, schema mismatches, question-framing "
            "issues — anything a human should see to improve the "
            "setup. Use this for observations about the SYSTEM, not "
            "for things you should figure out yourself (don't use it "
            "to say 'I'd rather not do this' or 'this is hard'). "
            "The event lands on the trace and the operator reviews "
            "later; runs continue normally. Severity ladder: "
            "`info` = nice-to-improve, `warn` = real friction worth "
            "addressing, `blocker` = the setup made it impossible to "
            "do the task well."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "subject": {
                    "type": "string",
                    "description": (
                        "What this is about — name the specific thing "
                        "(e.g. 'web_research system prompt', "
                        "'DelegateConfig.tools enum', 'seed_page_ids 3 of 5')."
                    ),
                },
                "detail": {
                    "type": "string",
                    "description": "What's off / what could improve. One short paragraph.",
                },
                "suggestion": {
                    "type": "string",
                    "description": "Optional concrete fix — what change you'd make.",
                },
                "severity": {
                    "type": "string",
                    "enum": ["info", "warn", "blocker"],
                    "description": "Severity. Defaults to `info`.",
                },
            },
            "required": ["subject", "detail"],
            "additionalProperties": False,
        },
        fn=fn,
    )


register_direct_tool(LOAD_PAGE_TOOL_NAME, build_load_page_tool)
register_direct_tool(CREATE_PAGE_TOOL_NAME, build_create_page_tool)
register_direct_tool(READ_ARTIFACT_TOOL_NAME, build_read_artifact_tool)
register_direct_tool(RECORD_OPERATOR_FEEDBACK_TOOL_NAME, build_record_operator_feedback_tool)
