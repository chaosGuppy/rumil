"""WebResearchSubroutine — search-and-scrape agent loop.

A specialised SR for evidence-gathering. Combines:

- Anthropic's ``web_search`` server tool (snippet-level discovery).
- A custom ``fetch_url`` client tool wrapping :func:`rumil.scraper.scrape_url`
  (full-text retrieval into a per-spawn fetch store).
- A custom ``read_fetched`` client tool for re-reading a previously
  fetched page in full (the ``fetch_url`` result returns just an
  excerpt + ``source_id`` to keep the SR's persistent thread compact).

On completion every fetched page is folded into the run's
:class:`ArtifactStore` as a separate ``produces`` entry keyed
``source/<sha8(url)>``, so mainline gets one artifact per source plus
the SR's own synthesis. Mainline can pull any of them back in via its
``read_artifact`` / ``search_artifacts`` tools or by passing
``include_artifacts`` on a follow-up spawn.

Distinct kind from ``freeform_agent`` because:

- The tool list is fixed (web_search + fetch_url + read_fetched), so
  ``allowed_tool_names`` doesn't apply.
- ``produces`` shape is multi-key (one per source), where FreeformAgent
  always emits a single empty-keyed artifact.
- Each tool binds to per-spawn fetch state — the registry pattern (one
  factory keyed by name) doesn't fit.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rumil.llm import Tool
from rumil.model_config import ModelConfig
from rumil.orchestrators.simple_spine.agent_loop import thin_agent_loop
from rumil.orchestrators.simple_spine.subroutines.base import (
    SpawnCtx,
    SubroutineBase,
    SubroutineResult,
    load_prompt,
    resolve_spawn_clock,
    sha8,
)
from rumil.scraper import ScrapedPage, scrape_url

log = logging.getLogger(__name__)

_FETCH_INLINE_EXCERPT_CHARS = 3000
_FETCH_MAX_USES_PER_SPAWN = 8


@dataclass
class _FetchedSource:
    """One scraped page bound to a per-spawn ``source_id``."""

    source_id: str
    url: str
    title: str
    content: str
    fetched_at: str


class _FetchStore:
    """Per-spawn store of fetched pages.

    Keyed by ``source_id`` (sha8 of the URL); also carries an url→id
    reverse index so re-fetching the same url within a spawn returns
    the existing source instead of double-scraping.
    """

    def __init__(self) -> None:
        self._by_id: dict[str, _FetchedSource] = {}
        self._url_to_id: dict[str, str] = {}

    def has(self, url: str) -> bool:
        return url in self._url_to_id

    def get(self, source_id: str) -> _FetchedSource | None:
        return self._by_id.get(source_id)

    def get_by_url(self, url: str) -> _FetchedSource | None:
        sid = self._url_to_id.get(url)
        return self._by_id.get(sid) if sid else None

    def add(self, scraped: ScrapedPage) -> _FetchedSource:
        source_id = hashlib.sha256(scraped.url.encode("utf-8")).hexdigest()[:8]
        if source_id in self._by_id:
            return self._by_id[source_id]
        entry = _FetchedSource(
            source_id=source_id,
            url=scraped.url,
            title=scraped.title or scraped.url,
            content=scraped.content,
            fetched_at=scraped.fetched_at,
        )
        self._by_id[source_id] = entry
        self._url_to_id[scraped.url] = source_id
        return entry

    def all(self) -> list[_FetchedSource]:
        return list(self._by_id.values())


def _build_fetch_url_tool(store: _FetchStore) -> Tool:
    async def fn(args: dict) -> str:
        url = str(args.get("url", "")).strip()
        if not url:
            return "Error: fetch_url requires a non-empty `url`."
        if not (url.startswith("http://") or url.startswith("https://")):
            return f"Error: fetch_url url must start with http:// or https://, got {url!r}."
        existing = store.get_by_url(url)
        if existing is not None:
            excerpt = existing.content[:_FETCH_INLINE_EXCERPT_CHARS]
            return (
                f"[already fetched] source_id=`{existing.source_id}`\n"
                f"title: {existing.title}\n"
                f"chars: {len(existing.content):,}\n\n"
                f"--- excerpt (first {len(excerpt):,} chars) ---\n{excerpt}"
            )
        try:
            scraped = await scrape_url(url)
        except Exception as e:
            log.exception("fetch_url scrape failed for %s", url)
            return f"Error: scrape failed for {url}: {type(e).__name__}: {e}"
        if scraped is None:
            return (
                f"Error: scrape returned no content for {url} "
                "(404, paywall, JS-only page, or scraper outage)."
            )
        entry = store.add(scraped)
        excerpt = entry.content[:_FETCH_INLINE_EXCERPT_CHARS]
        truncated_note = (
            f"\n\n[showing first {len(excerpt):,} of {len(entry.content):,} chars; "
            f"call `read_fetched` with source_id=`{entry.source_id}` for the full text]"
            if len(entry.content) > len(excerpt)
            else ""
        )
        return (
            f"Fetched. source_id=`{entry.source_id}`\n"
            f"title: {entry.title}\n"
            f"url: {entry.url}\n"
            f"chars: {len(entry.content):,}\n\n"
            f"--- excerpt ---\n{excerpt}{truncated_note}"
        )

    return Tool(
        name="fetch_url",
        description=(
            "Scrape a URL and return an excerpt + a stable `source_id`. The "
            "full text is stored in this spawn's fetch store and surfaced as "
            "an artifact at spawn end so mainline (and follow-up spawns) can "
            "read it. Use after `web_search` surfaces a promising URL — "
            "snippets often miss the load-bearing detail. Re-fetching the "
            "same URL is a no-op and returns the existing source_id."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Absolute http(s):// URL to scrape.",
                },
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        fn=fn,
    )


def _build_read_fetched_tool(store: _FetchStore) -> Tool:
    async def fn(args: dict) -> str:
        source_id = str(args.get("source_id", "")).strip()
        if not source_id:
            return "Error: read_fetched requires `source_id`."
        entry = store.get(source_id)
        if entry is None:
            available = sorted(s.source_id for s in store.all())
            return (
                f"Error: no source with source_id={source_id!r}. "
                f"Available source_ids in this spawn: {available or '(none)'}."
            )
        return (
            f"source_id=`{entry.source_id}`\n"
            f"title: {entry.title}\n"
            f"url: {entry.url}\n"
            f"fetched_at: {entry.fetched_at}\n\n"
            f"--- full content ({len(entry.content):,} chars) ---\n{entry.content}"
        )

    return Tool(
        name="read_fetched",
        description=(
            "Return the full text of a previously fetched source by "
            "`source_id` (from a prior `fetch_url` result in this spawn). "
            "Use when the fetch_url excerpt was thin and the load-bearing "
            "passage is later in the document."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "source_id": {
                    "type": "string",
                    "description": "8-char source_id returned by fetch_url.",
                },
            },
            "required": ["source_id"],
            "additionalProperties": False,
        },
        fn=fn,
    )


@dataclass(frozen=True, kw_only=True)
class WebResearchSubroutine(SubroutineBase):
    """Search + scrape SR. See module docstring."""

    sys_prompt: str
    user_prompt_template: str
    model: str
    max_rounds: int = 6
    max_tokens: int = 4096
    web_search_max_uses: int = 5
    allowed_domains: tuple[str, ...] = ()
    # Anthropic prompt caching. Default True — web_research is multi-round
    # by nature (search → fetch → re-search), so the system prompt + early
    # turns get reused across rounds at cache_read rates.
    cache: bool = True
    sys_prompt_path: str | Path | None = None
    overridable: frozenset[str] = field(
        default_factory=lambda: frozenset({"intent", "additional_context"})
    )

    def __post_init__(self) -> None:
        if self.max_rounds < 1:
            raise ValueError(f"max_rounds must be >= 1, got {self.max_rounds}")
        if self.web_search_max_uses < 1:
            raise ValueError(f"web_search_max_uses must be >= 1, got {self.web_search_max_uses}")
        if self.sys_prompt_path is not None:
            object.__setattr__(
                self,
                "sys_prompt",
                load_prompt(self.sys_prompt_path, self.sys_prompt),
            )

    def fingerprint(self) -> Mapping[str, Any]:
        out = dict(super().fingerprint())
        out["kind"] = "web_research"
        out["model"] = self.model
        out["sys_prompt_hash"] = sha8(self.sys_prompt)
        out["user_prompt_template_hash"] = sha8(self.user_prompt_template)
        out["max_rounds"] = self.max_rounds
        out["max_tokens"] = self.max_tokens
        out["web_search_max_uses"] = self.web_search_max_uses
        out["allowed_domains"] = sorted(self.allowed_domains)
        out["cache"] = self.cache
        return out

    def _default_intent_description(self) -> str:
        return (
            "Focused topic, question, or claim to research externally. "
            "The agent will issue 1–3 web_search queries, scrape promising "
            "URLs, and return a synthesis citing source_ids."
        )

    def _extra_schema_properties(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if "max_rounds" in self.overridable:
            out["max_rounds"] = {
                "type": "integer",
                "minimum": 1,
                "maximum": self.max_rounds,
                "description": (f"Cap rounds for this spawn (default {self.max_rounds})."),
            }
        return out

    def _build_server_tool_defs(self) -> list[dict]:
        web_search: dict = {
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": self.web_search_max_uses,
        }
        if self.allowed_domains:
            web_search["allowed_domains"] = list(self.allowed_domains)
        return [web_search]

    async def run(self, ctx: SpawnCtx, overrides: Mapping[str, Any]) -> SubroutineResult:
        sys_prompt = self.apply_assumptions(self.sys_prompt, ctx)
        max_rounds_override = overrides.get("max_rounds")
        max_rounds = (
            int(max_rounds_override)
            if max_rounds_override is not None and "max_rounds" in self.overridable
            else self.max_rounds
        )
        intent = str(overrides.get("intent", ""))
        additional_context = str(overrides.get("additional_context", ""))
        try:
            user_message = self.user_prompt_template.format(
                intent=intent,
                additional_context=additional_context,
            )
        except KeyError as e:
            raise ValueError(
                f"web_research {self.name!r}: user_prompt_template references unknown key {e}"
            ) from e

        artifact_block = self.render_artifact_block(ctx)
        if artifact_block:
            user_message = artifact_block + "\n" + user_message

        store = _FetchStore()
        client_tools: Sequence[Tool] = (
            _build_fetch_url_tool(store),
            _build_read_fetched_tool(store),
        )
        server_tool_defs = self._build_server_tool_defs()

        cfg = ModelConfig(temperature=1.0, max_tokens=self.max_tokens)
        messages: list[dict] = [{"role": "user", "content": user_message}]
        spawn_clock = resolve_spawn_clock(
            ctx.budget_clock,
            base_cap=self.base_cost_cap_usd,
            override_cap=overrides.get("cost_cap_usd")
            if "cost_cap_usd" in self.overridable
            else None,
        )
        result = await thin_agent_loop(
            system_prompt=sys_prompt,
            messages=messages,
            tools=client_tools,
            model=self.model,
            model_config=cfg,
            db=ctx.db,
            call_id=ctx.parent_call_id,
            phase=f"spawn:{self.name}",
            budget_clock=spawn_clock,
            max_rounds=max_rounds,
            cache=self.cache,
            server_tool_defs=server_tool_defs,
        )

        fetched = store.all()
        produces: dict[str, str] = {"summary": result.final_text}
        for src in fetched:
            body = (
                f"# {src.title}\n"
                f"url: {src.url}\n"
                f"source_id: {src.source_id}\n"
                f"fetched_at: {src.fetched_at}\n\n"
                f"{src.content}"
            )
            produces[f"source/{src.source_id}"] = body

        sources_lines = (
            "\n".join(f"- `source/{s.source_id}` — {s.title} ({s.url})" for s in fetched)
            if fetched
            else "(no sources fetched)"
        )
        text_summary = (
            f"# {self.name}\n"
            f"_(rounds={result.rounds}, stopped_because={result.stopped_because}, "
            f"sources_fetched={len(fetched)})_\n\n"
            f"## Synthesis\n\n{result.final_text}\n\n"
            f"## Fetched sources (artifact sub-keys)\n\n{sources_lines}"
        )
        return SubroutineResult(
            text_summary=text_summary,
            extra={
                "rounds": result.rounds,
                "stopped_because": result.stopped_because,
                "tool_call_count": len(result.tool_calls),
                "sources_fetched": len(fetched),
            },
            produces=produces,
        )
