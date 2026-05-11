"""Web fetch tools for axon delegates — search-and-scrape kit.

Two direct tools registered for use inside delegate inner loops (NOT
mainline). Configure can list them in ``cfg.tools`` alongside the
Anthropic ``web_search`` server tool to give a delegate the
search-then-scrape capability simple_spine's ``WebResearchSubroutine``
had.

- ``fetch_url`` — scrape a URL via :func:`rumil.scraper.scrape_url`,
  return an excerpt + ``source_id`` (sha8 of url), and persist the full
  body as a ``source/<sha8>`` artifact in the run's ArtifactStore.
- ``read_fetched`` — return the full text of a previously fetched
  source by ``source_id`` (cheaper than re-fetching when the excerpt
  was thin).

The fetch store is per-run (lives on :class:`DirectToolCtx`), so re-
fetching the same URL across delegates in one run is a no-op cache hit.
Fetched bodies become first-class artifacts so other delegates and
mainline can ``read_artifact`` them under the same ``source/<sha8>``
key.
"""

from __future__ import annotations

import hashlib
import logging

from rumil.llm import Tool
from rumil.orchestrators.axon.direct_tools import get_direct_tool_ctx
from rumil.orchestrators.axon.tools import register_direct_tool
from rumil.scraper import ScrapedPage, scrape_url

log = logging.getLogger(__name__)

FETCH_URL_TOOL_NAME = "fetch_url"
READ_FETCHED_TOOL_NAME = "read_fetched"
ARTIFACT_KEY_PREFIX = "source/"

_FETCH_INLINE_EXCERPT_CHARS = 3000


class WebFetchStore:
    """Per-run store of scraped pages keyed by sha8(url).

    Carries an url→source_id reverse index so re-fetching the same url
    within the run returns the existing source instead of double-
    scraping. Fetched pages are also written to the run's ArtifactStore
    under ``source/<sha8>`` keys at fetch time.
    """

    def __init__(self) -> None:
        self._by_id: dict[str, ScrapedPage] = {}
        self._url_to_id: dict[str, str] = {}

    def get(self, source_id: str) -> ScrapedPage | None:
        return self._by_id.get(source_id)

    def get_by_url(self, url: str) -> tuple[str, ScrapedPage] | None:
        sid = self._url_to_id.get(url)
        if sid is None:
            return None
        return sid, self._by_id[sid]

    def add(self, scraped: ScrapedPage) -> str:
        source_id = hashlib.sha256(scraped.url.encode("utf-8")).hexdigest()[:8]
        if source_id not in self._by_id:
            self._by_id[source_id] = scraped
            self._url_to_id[scraped.url] = source_id
        return source_id

    def all_ids(self) -> list[str]:
        return list(self._by_id)


def _artifact_body(source_id: str, scraped: ScrapedPage) -> str:
    return (
        f"# {scraped.title}\n"
        f"url: {scraped.url}\n"
        f"source_id: {source_id}\n"
        f"fetched_at: {scraped.fetched_at}\n\n"
        f"{scraped.content}"
    )


def build_fetch_url_tool() -> Tool:
    async def fn(args: dict) -> str:
        ctx = get_direct_tool_ctx()
        if ctx.fetch_store is None:
            return (
                "Error: fetch_url requires the orchestrator to have wired a "
                "WebFetchStore into the DirectToolCtx; none is set."
            )
        url = str(args.get("url", "")).strip()
        if not url:
            return "Error: fetch_url requires a non-empty `url`."
        if not (url.startswith("http://") or url.startswith("https://")):
            return f"Error: fetch_url url must start with http:// or https://, got {url!r}."
        existing = ctx.fetch_store.get_by_url(url)
        if existing is not None:
            sid, page = existing
            excerpt = page.content[:_FETCH_INLINE_EXCERPT_CHARS]
            return (
                f"[already fetched] source_id=`{sid}` "
                f"(artifact key `{ARTIFACT_KEY_PREFIX}{sid}`)\n"
                f"title: {page.title}\n"
                f"chars: {len(page.content):,}\n\n"
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
        source_id = ctx.fetch_store.add(scraped)
        artifact_key = f"{ARTIFACT_KEY_PREFIX}{source_id}"
        if ctx.artifacts is not None and artifact_key not in ctx.artifacts:
            ctx.artifacts.add(
                artifact_key,
                _artifact_body(source_id, scraped),
                produced_by="fetch_url",
                description=f"scraped page: {scraped.title}",
            )
        excerpt = scraped.content[:_FETCH_INLINE_EXCERPT_CHARS]
        truncated_note = (
            f"\n\n[showing first {len(excerpt):,} of {len(scraped.content):,} chars; "
            f"call `read_fetched` with source_id=`{source_id}` for the full text "
            f"or `read_artifact` with key=`{artifact_key}`]"
            if len(scraped.content) > len(excerpt)
            else ""
        )
        return (
            f"Fetched. source_id=`{source_id}` "
            f"(artifact key `{artifact_key}`)\n"
            f"title: {scraped.title}\n"
            f"url: {scraped.url}\n"
            f"chars: {len(scraped.content):,}\n\n"
            f"--- excerpt ---\n{excerpt}{truncated_note}"
        )

    return Tool(
        name=FETCH_URL_TOOL_NAME,
        description=(
            "Scrape a URL and return an excerpt + a stable `source_id`. "
            "The full text is stored in this run's fetch store and "
            "written as an artifact under key `source/<source_id>` so "
            "any sibling delegate or mainline can `read_artifact` it. "
            "Use after `web_search` surfaces a promising URL — snippets "
            "often miss the load-bearing detail. Re-fetching the same "
            "URL is a no-op and returns the existing source_id."
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


def build_read_fetched_tool() -> Tool:
    async def fn(args: dict) -> str:
        ctx = get_direct_tool_ctx()
        if ctx.fetch_store is None:
            return (
                "Error: read_fetched requires the orchestrator to have wired a "
                "WebFetchStore into the DirectToolCtx; none is set."
            )
        source_id = str(args.get("source_id", "")).strip()
        if not source_id:
            return "Error: read_fetched requires `source_id`."
        page = ctx.fetch_store.get(source_id)
        if page is None:
            available = sorted(ctx.fetch_store.all_ids())
            return (
                f"Error: no source with source_id={source_id!r}. "
                f"Available source_ids in this run: {available or '(none)'}."
            )
        return (
            f"source_id=`{source_id}`\n"
            f"title: {page.title}\n"
            f"url: {page.url}\n"
            f"fetched_at: {page.fetched_at}\n\n"
            f"--- full content ({len(page.content):,} chars) ---\n{page.content}"
        )

    return Tool(
        name=READ_FETCHED_TOOL_NAME,
        description=(
            "Return the full text of a previously fetched source by "
            "`source_id` (from a prior `fetch_url` result in this run). "
            "Use when the fetch_url excerpt was thin and the load-"
            "bearing passage is later in the document. Equivalent to "
            "`read_artifact` with key `source/<source_id>`; either works."
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


register_direct_tool(FETCH_URL_TOOL_NAME, build_fetch_url_tool)
register_direct_tool(READ_FETCHED_TOOL_NAME, build_read_fetched_tool)
