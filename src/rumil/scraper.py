"""Lightweight URL scraper using httpx + BeautifulSoup."""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

MAX_CONTENT_CHARS = 50_000
TIMEOUT_SECONDS = 15
USER_AGENT = (
    'Mozilla/5.0 (compatible; Rumil/0.1; +https://github.com/chaosGuppy/differential)'
)


@dataclass
class ScrapedPage:
    url: str
    title: str
    content: str
    fetched_at: str


async def scrape_url(url: str) -> ScrapedPage | None:
    """Fetch and extract text content from a URL.

    Returns a ScrapedPage on success, or None on any failure.
    """
    try:
        async with httpx.AsyncClient(
            timeout=TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={'User-Agent': USER_AGENT},
        ) as client:
            response = await client.get(url)
            response.raise_for_status()

        soup = BeautifulSoup(response.text, 'html.parser')

        for tag in soup.find_all(['script', 'style', 'nav', 'footer']):
            tag.decompose()

        title = soup.title.get_text(strip=True) if soup.title else url
        content = soup.get_text(separator='\n', strip=True)
        content = content[:MAX_CONTENT_CHARS]

        return ScrapedPage(
            url=url,
            title=title,
            content=content,
            fetched_at=datetime.now(UTC).isoformat(),
        )
    except Exception:
        log.warning('Failed to scrape URL: %s', url, exc_info=True)
        return None
