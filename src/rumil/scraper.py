"""URL scraper using Jina Reader API (https://r.jina.ai/)."""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from rumil.settings import get_settings

log = logging.getLogger(__name__)

JINA_READER_BASE = "https://r.jina.ai/"
MAX_CONTENT_CHARS = 50_000
TIMEOUT_SECONDS = 30


@dataclass
class ScrapedPage:
    url: str
    title: str
    content: str
    fetched_at: str


async def scrape_url(url: str, *, max_chars: int | None = None) -> ScrapedPage | None:
    """Fetch and extract text content from a URL via Jina Reader.

    Returns a ScrapedPage on success, or None on any failure.
    *max_chars* overrides the default truncation limit (MAX_CONTENT_CHARS).
    """
    limit = max_chars if max_chars is not None else MAX_CONTENT_CHARS
    try:
        headers: dict[str, str] = {"Accept": "application/json"}
        api_key = get_settings().jina_api_key
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            response = await client.get(
                f"{JINA_READER_BASE}{url}",
                headers=headers,
            )
            response.raise_for_status()

        data = response.json().get("data")
        if not data or not data.get("content"):
            log.warning("Jina Reader returned no content for URL: %s", url)
            return None

        content = data["content"][:limit]
        title = data.get("title") or url

        return ScrapedPage(
            url=data.get("url", url),
            title=title,
            content=content,
            fetched_at=datetime.now(UTC).isoformat(),
        )
    except Exception:
        log.warning("Failed to scrape URL: %s", url, exc_info=True)
        return None
