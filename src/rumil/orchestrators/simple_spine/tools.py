"""Tool registry + spawn-tool / finalize-tool factory.

The mainline agent's toolkit is composed at run time:
- one spawn tool per ``SubroutineDef`` in the config's library
- a ``finalize`` tool emitting the final answer

The same registry is reused by ``FreeformAgentSubroutine`` to wire its
own tools at spawn time. Registered tool factories produce
:class:`rumil.llm.Tool` instances given a ``SpawnCtx``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING

from rumil.context import build_embedding_based_context
from rumil.llm import Tool

if TYPE_CHECKING:
    from rumil.orchestrators.simple_spine.artifacts import ArtifactStore
    from rumil.orchestrators.simple_spine.subroutines.base import SpawnCtx

log = logging.getLogger(__name__)


ToolFactory = Callable[["SpawnCtx"], Tool]

_REGISTRY: dict[str, ToolFactory] = {}


def register_tool(name: str, factory: ToolFactory) -> None:
    """Register a tool factory under ``name``.

    Idempotent: re-registering the same name silently overwrites — this
    keeps test fixtures and module-level register calls safe to re-run.
    """
    _REGISTRY[name] = factory


def resolve_tools(names: Sequence[str], ctx: SpawnCtx) -> list[Tool]:
    """Build a list of :class:`Tool` instances for the given names.

    Unknown names raise — silent omission would let a config typo make
    a tool quietly disappear from the spawned agent's toolkit.
    """
    out: list[Tool] = []
    for n in names:
        factory = _REGISTRY.get(n)
        if factory is None:
            available = sorted(_REGISTRY)
            raise KeyError(f"unknown tool {n!r}; registered tools: {available}")
        out.append(factory(ctx))
    return out


def make_finalize_tool(
    on_finalize: Callable[[str], Awaitable[str]],
) -> Tool:
    """Build the ``finalize`` tool the mainline agent calls to terminate.

    ``on_finalize`` receives the answer text and returns a tool-result
    string (typically a confirmation; the orchestrator inspects state
    set by ``on_finalize`` to decide whether to break the loop).
    """

    async def fn(args: dict) -> str:
        answer = str(args.get("answer", "")).strip()
        if not answer:
            return "Error: finalize requires a non-empty `answer` field."
        return await on_finalize(answer)

    return Tool(
        name="finalize",
        description=(
            "Emit the final deliverable for this run and terminate the loop. "
            "Call this when you have produced the answer that satisfies the "
            "output guidance, when further spawns would not improve the "
            "deliverable, or when budget pressure forces it. Pass the full "
            "answer text in `answer` — the harness extracts it verbatim."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "answer": {
                    "type": "string",
                    "description": "The full final deliverable text.",
                },
                "reason": {
                    "type": "string",
                    "description": "Brief note on why finalizing now (for the trace).",
                },
            },
            "required": ["answer"],
            "additionalProperties": False,
        },
        fn=fn,
    )


def make_workspace_search_tool(ctx: SpawnCtx) -> Tool:
    """Embedding-similarity search over the active workspace.

    Wraps :func:`build_embedding_based_context` against the spawn's scope
    question. Returns the rendered tiered context block (full / abstract /
    summary / distillation) for whatever the agent passed as ``query``.
    """

    async def fn(args: dict) -> str:
        query = str(args.get("query", "")).strip()
        if not query:
            return "Error: workspace_search requires a non-empty `query`."
        try:
            result = await build_embedding_based_context(
                query,
                ctx.db,
                scope_question_id=ctx.question_id,
            )
        except Exception as e:
            log.exception("workspace_search failed")
            return f"Error: workspace search failed: {type(e).__name__}: {e}"
        return result.context_text or "[no relevant pages found for this query]"

    return Tool(
        name="workspace_search",
        description=(
            "Search the workspace via embedding similarity for pages "
            "relevant to a free-text query. Returns rendered page snippets "
            "across tiers (full / abstract / summary). Use to surface "
            "considerations, claims, or related questions that bear on the "
            "scope question. Issue several queries if the first is thin — "
            "varying phrasing often surfaces different pages."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Free-text query to embed. Phrase as a question or "
                        "topic statement; the embedding model handles both."
                    ),
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        fn=fn,
    )


register_tool("workspace_search", make_workspace_search_tool)


_READ_ARTIFACT_MAX_CHARS = 30_000
_SEARCH_ARTIFACTS_TOP_K = 5
_SEARCH_ARTIFACTS_SNIPPET_CHARS = 400


def make_read_artifact_tool(store: ArtifactStore) -> Tool:
    """Mainline tool: pull a single artifact's full text into context.

    Caps the returned content at ``_READ_ARTIFACT_MAX_CHARS`` to keep one
    accidental ``read_artifact`` on a 50k-char scrape from blowing out
    the persistent thread. The cap notes how many chars were truncated
    so the model can decide whether to ask for a different artifact or a
    re-scrape via web_research.
    """

    async def fn(args: dict) -> str:
        key = str(args.get("key", "")).strip()
        if not key:
            return "Error: read_artifact requires `key`."
        art = store.get(key)
        if art is None:
            available = store.list_keys()
            preview = available[:30]
            tail = "" if len(available) <= 30 else f"\n…and {len(available) - 30} more"
            return (
                f"Error: no artifact at key {key!r}. "
                f"Available keys ({len(available)}):\n- " + "\n- ".join(preview) + tail
            )
        body = art.text
        truncated_note = ""
        if len(body) > _READ_ARTIFACT_MAX_CHARS:
            head = body[:_READ_ARTIFACT_MAX_CHARS]
            truncated_note = (
                f"\n\n[truncated: showed {_READ_ARTIFACT_MAX_CHARS:,} of {len(body):,} chars]"
            )
            body = head
        provenance = (
            "input"
            if art.produced_by == "input"
            else f"spawn:{art.produced_by}"
            + (f" round {art.round_idx}" if art.round_idx is not None else "")
        )
        return (
            f"artifact `{art.key}` ({len(art.text):,} chars, from {provenance})\n\n"
            f"{body}{truncated_note}"
        )

    return Tool(
        name="read_artifact",
        description=(
            "Return the full text of a named artifact. Use to pull a "
            "scraped source, a prior spawn's output, or a caller-seeded "
            "blob into your context when you need to read it directly. "
            f"Capped at {_READ_ARTIFACT_MAX_CHARS:,} chars; longer "
            "artifacts are truncated with a note."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": (
                        "Artifact key (announced in tool_result messages "
                        "as spawns produce them; format "
                        "`<sub_name>/<spawn_id>[/<sub_key>]`)."
                    ),
                },
            },
            "required": ["key"],
            "additionalProperties": False,
        },
        fn=fn,
    )


def _score_artifact(text: str, terms: Sequence[str]) -> int:
    if not terms:
        return 0
    lowered = text.lower()
    return sum(lowered.count(t) for t in terms)


def _find_snippet(text: str, terms: Sequence[str], window: int) -> str:
    lowered = text.lower()
    best_idx = -1
    for t in terms:
        idx = lowered.find(t)
        if idx != -1 and (best_idx == -1 or idx < best_idx):
            best_idx = idx
    if best_idx == -1:
        return text[:window]
    half = window // 2
    start = max(best_idx - half, 0)
    end = min(start + window, len(text))
    snippet = text[start:end]
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{snippet}{suffix}"


def make_search_artifacts_tool(store: ArtifactStore) -> Tool:
    """Mainline tool: keyword-rank artifacts and return top-K snippets.

    Tokenises the query into whitespace-separated terms (lowercased),
    scores each artifact by sum of term occurrences, returns the top
    ``_SEARCH_ARTIFACTS_TOP_K`` with a windowed snippet around the
    first matched term. Substring v1 because per-run artifact counts
    are small (≤ ~50) and embedding lookups would add a DB roundtrip
    per call. If artifact volume grows, swap for embedding similarity.
    """

    async def fn(args: dict) -> str:
        query = str(args.get("query", "")).strip()
        if not query:
            return "Error: search_artifacts requires a non-empty `query`."
        terms = [t.lower() for t in query.split() if t.strip()]
        if not terms:
            return "Error: query has no usable terms after tokenisation."
        keys = store.list_keys()
        if not keys:
            return "[no artifacts in this run yet]"
        scored: list[tuple[int, str]] = []
        for k in keys:
            art = store.get(k)
            if art is None:
                continue
            score = _score_artifact(art.text, terms)
            if score > 0:
                scored.append((score, k))
        scored.sort(key=lambda p: -p[0])
        if not scored:
            return f"[no artifact matched terms: {terms}]"
        top = scored[:_SEARCH_ARTIFACTS_TOP_K]
        lines: list[str] = [f"## search_artifacts: {len(top)} of {len(scored)} matches"]
        for score, k in top:
            art = store.get(k)
            if art is None:
                continue
            snippet = _find_snippet(art.text, terms, _SEARCH_ARTIFACTS_SNIPPET_CHARS)
            lines.append(f"\n### `{k}` (score={score}, {len(art.text):,} chars)\n{snippet}")
        return "\n".join(lines)

    return Tool(
        name="search_artifacts",
        description=(
            "Keyword-rank artifacts in this run by total term occurrences "
            "and return the top matches with windowed snippets around the "
            "first hit. Cheap; substring-based, not embedding. Use to find "
            "which fetched source / prior spawn output mentions a term "
            "before deciding what to `read_artifact`."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Whitespace-separated keywords. Lowercased and "
                        "scored by sum of occurrences across artifacts."
                    ),
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        fn=fn,
    )
