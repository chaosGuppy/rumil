"""Fetch Redwood Research blog posts from Substack.

Uses the RSS feed's ``content:encoded`` payload — no per-post fetch is
needed because Substack ships the full post body in the feed. Raw HTML
is cached to ``raw_html/<essay_id>.html`` for parser debugging.

Substack body HTML uses standard tags (``<p>``, ``<h1>``-``<h4>``,
``<ul>``, ``<ol>``, ``<blockquote>``, ``<strong>``, ``<em>``, ``<sup>``).
Two source-specific quirks are handled in the parser:

  * Inline footnote markers render as ``<sup>[N]</sup>`` — stripped.
  * The footnote *bodies* render as a trailing ``<ol>`` whose items link
    back to ``#fnref...``. That block is detected and removed before
    markdown rendering so the essay body doesn't leak an orphan list.
"""

from __future__ import annotations

import json
import pathlib
import re
import xml.etree.ElementTree as ET

import bs4
import httpx

from versus.essay import (
    SCHEMA_VERSION,
    Block,
    Essay,
    blocks_to_markdown,
    clean_blocks,
    namespaced_id,
)
from versus.sources import _html_common as hc

SOURCE_ID = "redwood"
RSS_URL = "https://blog.redwoodresearch.org/feed"


def _parse_rss(xml_text: str) -> list[dict]:
    ns = {
        "dc": "http://purl.org/dc/elements/1.1/",
        "content": "http://purl.org/rss/1.0/modules/content/",
    }
    root = ET.fromstring(xml_text)
    items = []
    for item in root.iter("item"):
        items.append(
            {
                "title": (item.findtext("title") or "").strip(),
                "link": (item.findtext("link") or "").strip(),
                "guid": (item.findtext("guid") or "").strip(),
                "pub_date": (item.findtext("pubDate") or "").strip(),
                "author": (item.findtext("dc:creator", namespaces=ns) or "").strip(),
                "content": (item.findtext("content:encoded", namespaces=ns) or "").strip(),
            }
        )
    return items


def _is_footnote_list(ol: bs4.element.Tag) -> bool:
    """Return True if the ``<ol>`` looks like Substack's footnote-bodies block.

    Substack footnotes render as a final ``<ol>`` where each ``<li>`` opens
    with a back-reference link pointing at ``#fnref...``. If every item in
    the list contains such a link, treat the whole ``<ol>`` as footnotes.
    """
    lis = ol.find_all("li", recursive=False)
    if not lis:
        return False
    for li in lis:
        a = li.find("a", href=True)
        if not a or not a["href"].startswith("#fnref"):
            return False
    return True


def _parse_body_html(html: str) -> tuple[list[Block], int]:
    soup = bs4.BeautifulSoup(html, "lxml")

    # Substack has two footnote variants:
    #   A) older: inline <sup>[N]</sup> markers, bodies in a trailing <ol>
    #      whose items back-link to #fnref...
    #   B) newer: <a class="footnote-anchor" href="#footnote-N">N</a> inline,
    #      bodies in <div class="footnote" data-component-name="FootnoteToDOM">
    #      blocks at the end.
    # Strip both variants entirely so the body markdown has no orphan digits.

    # Variant A: trailing <ol> of footnote bodies.
    top_ols = [ol for ol in soup.find_all("ol") if not ol.find_parent(("ul", "ol", "li"))]
    for ol in reversed(top_ols):
        if _is_footnote_list(ol):
            ol.decompose()
            break

    # Variant B: <div class="footnote"> bodies anywhere (usually at the end).
    for div in soup.find_all("div", class_=re.compile(r"(?:^|\s)footnote(?:\s|$)")):
        div.decompose()

    # Inline markers: <sup>, <a class="footnote-anchor">, Substack "^" back-arrows.
    for sup in soup.find_all("sup"):
        sup.decompose()
    for a in soup.find_all("a", class_=re.compile(r"footnote-anchor|footnote-number")):
        a.decompose()

    # Count image-like elements before stripping. Substack wraps figures in
    # ``captioned-image-container`` — count those as the unit of meaning
    # rather than individual <img>s (a captioned figure = one image).
    image_count = len(soup.find_all("figure")) + len(
        soup.find_all("div", class_=re.compile(r"captioned-image-container"))
    )
    # Fall back to counting raw <img> tags only when neither figure nor
    # captioned-image-container wraps them — avoids double-counting.
    bare_imgs = [
        img
        for img in soup.find_all("img")
        if not img.find_parent("figure")
        and not img.find_parent("div", class_=re.compile(r"captioned-image-container|image2-inset"))
    ]
    image_count += len(bare_imgs)

    # Drop image/figure blocks and Substack captioned-image containers — we
    # don't surface images and don't want caption strings leaking as paragraphs.
    for tag in soup.find_all(("figure", "img")):
        tag.decompose()
    for div in soup.find_all("div", class_=re.compile(r"(captioned-image-container|image2-inset)")):
        div.decompose()

    blocks: list[Block] = []
    seen: set[int] = set()
    for el in soup.descendants:
        if not isinstance(el, bs4.element.Tag):
            continue
        if id(el) in seen:
            continue
        name = el.name
        if name in ("h1", "h2", "h3"):
            text = hc.render_text(el)
            if text:
                blocks.append(Block(type=name, text=text))
            _mark_subtree(el, seen)
        elif name == "h4":
            # Treat h4 as h3 for consistency with the shared heading model.
            text = hc.render_text(el)
            if text:
                blocks.append(Block(type="h3", text=text))
            _mark_subtree(el, seen)
        elif name in ("ul", "ol"):
            if el.find_parent(("ul", "ol", "li")):
                continue
            text = hc.render_list(el, ordered=(name == "ol"))
            if text:
                blocks.append(Block(type="p", text=text))
            _mark_subtree(el, seen)
        elif name == "blockquote":
            text = hc.render_blockquote(el)
            if text:
                blocks.append(Block(type="p", text=text))
            _mark_subtree(el, seen)
        elif name == "p":
            if el.find_parent(("li", "blockquote")):
                continue
            if hc.is_caption_only_para(el):
                _mark_subtree(el, seen)
                continue
            text = hc.render_text(el)
            if text and text.lower() != "image":
                blocks.append(Block(type="p", text=text))
            _mark_subtree(el, seen)
    return blocks, image_count


def _mark_subtree(el: bs4.element.Tag, seen: set[int]) -> None:
    for child in el.descendants:
        if isinstance(child, bs4.element.Tag):
            seen.add(id(child))
    seen.add(id(el))


def _slug_from_url(url: str) -> str:
    # Substack URLs look like .../p/<slug>
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    return tail


def fetch(
    *,
    source_cfg,
    cache_dir: pathlib.Path,
    raw_html_dir: pathlib.Path,
    client: httpx.Client,
) -> list[Essay]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    raw_html_dir.mkdir(parents=True, exist_ok=True)

    rss = client.get(RSS_URL)
    rss.raise_for_status()
    items = _parse_rss(rss.text)[: source_cfg.max_recent]

    essays: list[Essay] = []
    for item in items:
        slug = _slug_from_url(item["guid"] or item["link"])
        essay_id = namespaced_id(SOURCE_ID, slug)
        json_path = cache_dir / f"{essay_id}.json"
        html_path = raw_html_dir / f"{essay_id}.html"

        if json_path.exists():
            with open(json_path) as f:
                d = json.load(f)
            if d.get("schema_version") == SCHEMA_VERSION and d.get("source_id") == SOURCE_ID:
                essays.append(
                    Essay(
                        id=d["id"],
                        source_id=d["source_id"],
                        url=d["url"],
                        title=d["title"],
                        author=d["author"],
                        pub_date=d["pub_date"],
                        blocks=[Block(**b) for b in d["blocks"]],
                        markdown=d["markdown"],
                        image_count=d.get("image_count", 0),
                        schema_version=d["schema_version"],
                    )
                )
                continue

        html = item["content"]
        if not html:
            print(f"[warn] redwood feed item has no content: {slug}")
            continue
        html_path.write_text(html)

        raw_blocks, image_count = _parse_body_html(html)
        if not raw_blocks:
            print(f"[warn] no blocks parsed for {essay_id}")
            continue
        blocks = clean_blocks(raw_blocks)
        markdown = blocks_to_markdown(blocks)

        essay = Essay(
            id=essay_id,
            source_id=SOURCE_ID,
            url=item["link"],
            title=item["title"],
            author=item["author"],
            pub_date=item["pub_date"],
            blocks=blocks,
            markdown=markdown,
            image_count=image_count,
        )
        with open(json_path, "w") as f:
            json.dump(essay.to_json(), f, indent=2)
        essays.append(essay)
    return essays
