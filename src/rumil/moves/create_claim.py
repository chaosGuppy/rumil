"""CREATE_CLAIM move: create an assertion with supporting reasoning."""

import logging
import re
from collections.abc import Sequence

from pydantic import Field

from rumil.database import DB
from rumil.models import (
    Call,
    LinkType,
    MoveType,
    Page,
    PageLayer,
    PageLink,
    PageType,
    Workspace,
)
from rumil.moves.base import (
    CreatePagePayload,
    MoveDef,
    MoveResult,
    create_page,
)
from rumil.moves.link_consideration import ConsiderationLinkFields
from rumil.scraper import scrape_url

log = logging.getLogger(__name__)


class CreateClaimPayload(CreatePagePayload):
    source_urls: Sequence[str] = Field(
        default_factory=list,
        description=(
            "URLs of web pages this claim is based on. Only use during web "
            "research after fetching or searching web pages. Each URL becomes "
            "a source page with a CITES link to this claim."
        ),
    )
    links: list[ConsiderationLinkFields] = Field(
        default_factory=list,
        description=(
            "Consideration links to create for this claim. Each entry links "
            "the new claim to a QUESTION it bears on, with a strength rating. "
            "Only use this for claim → question links. For claim → claim "
            "dependencies, make a separate link_depends_on call."
        ),
    )


async def execute(payload: CreateClaimPayload, call: Call, db: DB) -> MoveResult:
    result = await create_page(payload, call, db, PageType.CLAIM, PageLayer.SQUIDGY)
    if not result.created_page_id:
        return result

    for link_spec in payload.links:
        resolved = await db.resolve_page_id(link_spec.question_id)
        if not resolved:
            log.warning(
                "Inline consideration link skipped: question %s not found",
                link_spec.question_id,
            )
            continue

        target = await db.get_page(resolved)
        if target is None or target.page_type != PageType.QUESTION:
            log.warning(
                "Inline consideration link skipped: target %s is %s, expected question",
                resolved[:8],
                target.page_type.value if target else "missing",
            )
            continue

        await db.save_link(
            PageLink(
                from_page_id=result.created_page_id,
                to_page_id=resolved,
                link_type=LinkType.CONSIDERATION,
                strength=link_spec.strength,
                reasoning=link_spec.reasoning,
                role=link_spec.role,
            )
        )
        log.info(
            "Inline consideration linked: %s -> %s (%.1f)",
            result.created_page_id[:8],
            resolved[:8],
            link_spec.strength,
        )

    for sid in payload.source_urls:
        resolved = await db.resolve_page_id(sid)
        if not resolved:
            log.warning(
                "Citation link skipped: source %s not found",
                sid,
            )
            continue

        await db.save_link(
            PageLink(
                from_page_id=result.created_page_id,
                to_page_id=resolved,
                link_type=LinkType.CITES,
            )
        )
        log.info(
            "Citation linked: %s -> %s",
            result.created_page_id[:8],
            resolved[:8],
        )

    return result


MOVE = MoveDef(
    move_type=MoveType.CREATE_CLAIM,
    name="create_claim",
    description=(
        "Create a new claim — an assertion with supporting reasoning and "
        "epistemic status. The atomic unit of knowledge. Use the links field "
        "to simultaneously link this claim as a consideration on one or more "
        "questions."
    ),
    schema=CreateClaimPayload,
    execute=execute,
)


_URL_CITATION_RE = re.compile(r"\[(https?://[^\]\s]+)\]")


async def ensure_source_page(
    url: str,
    call: Call,
    db: DB,
    source_cache: dict[str, str],
) -> str | None:
    """Scrape *url* and create a SOURCE page, returning its page ID.

    Uses *source_cache* to avoid re-scraping URLs already seen in this run.
    """
    if url in source_cache:
        return source_cache[url]

    scraped = await scrape_url(url)
    if scraped is None:
        log.warning("Scrape failed for URL: %s", url)
        return None

    page = Page(
        page_type=PageType.SOURCE,
        layer=PageLayer.SQUIDGY,
        workspace=Workspace.RESEARCH,
        content=scraped.content,
        headline=scraped.title[:120],
        credence=5,
        robustness=1,
        provenance_model="scraper",
        provenance_call_type=call.call_type.value,
        provenance_call_id=call.id,
        extra={
            "url": url,
            "fetched_at": scraped.fetched_at,
            "char_count": len(scraped.content),
        },
    )
    await db.save_page(page)

    source_cache[url] = page.id
    log.info(
        "Source page created: %s -> %s (%s)",
        url[:60],
        page.id[:8],
        scraped.title[:60],
    )
    return page.id


def rewrite_url_citations(content: str, source_cache: dict[str, str]) -> str:
    """Replace ``[url]`` inline citations with ``[shortid]``.

    Uses slightly-forgiving matching: a trailing-slash difference is
    tolerated.  Raises ``ValueError`` for URLs not in *source_cache*.
    """

    def _normalize(url: str) -> str:
        return url.rstrip("/")

    normalized_lookup: dict[str, str] = {
        _normalize(url): page_id for url, page_id in source_cache.items()
    }

    unmatched: list[str] = []

    def _replace(m: re.Match[str]) -> str:
        url = m.group(1)
        page_id = normalized_lookup.get(_normalize(url))
        if page_id is not None:
            return f"[{page_id[:8]}]"
        unmatched.append(url)
        return m.group(0)

    rewritten = _URL_CITATION_RE.sub(_replace, content)
    if unmatched:
        raise ValueError(
            "Inline citation URLs do not match any scraped source page: "
            + ", ".join(unmatched)
            + ". Only cite URLs that are also listed in source_urls."
        )
    return rewritten


async def execute_with_source_creation(
    inp: dict,
    call: Call,
    db: DB,
    source_cache: dict[str, str],
) -> MoveResult:
    """Like ``execute()``, but resolves HTTP URLs in ``source_urls`` first.

    For each ``source_urls`` entry starting with ``http``, scrapes the URL
    and creates a SOURCE page.  Also rewrites ``[url]`` inline citations
    in content to ``[shortid]``.
    """
    source_urls = inp.get("source_urls", [])
    if source_urls:
        resolved: list[str] = []
        failed_urls: list[str] = []
        for sid in source_urls:
            if isinstance(sid, str) and sid.startswith("http"):
                page_id = await ensure_source_page(sid, call, db, source_cache)
                if page_id:
                    resolved.append(page_id)
                else:
                    failed_urls.append(sid)
            else:
                resolved.append(sid)
        if failed_urls:
            urls = ", ".join(failed_urls)
            return MoveResult(
                message=(
                    f"ERROR: Could not fetch the following source(s): {urls}. "
                    "Find a different, accessible source that supports "
                    "the same information, or modify the claim so it "
                    "does not rely on the inaccessible source(s)."
                ),
                created_page_id="",
            )
        inp = {**inp, "source_urls": resolved}

    content = inp.get("content", "")
    if content:
        try:
            inp = {**inp, "content": rewrite_url_citations(content, source_cache)}
        except ValueError as exc:
            return MoveResult(message=f"ERROR: {exc}", created_page_id="")

    payload = CreateClaimPayload(**inp)
    return await execute(payload, call, db)
