"""Fetch Joe Carlsmith's blog posts (joecarlsmith.com).

The RSS feed (``/rss.xml``) only includes post metadata — the full body
lives on the per-post page, so we fetch each URL and parse its HTML.

The site is a Gatsby build with stable structure:

  * ``<main class="site-main">`` is the outer container
  * body text sits inside ``<div class="single-essay__main">``
  * footnote markers inline are ``<sup class="article-reference">`` —
    stripped before parsing
  * footnote bodies live in a sibling ``<div class="single-essay__references">``
    outside the body div, so no trailing-footnote cleanup is needed
  * series navigation sits in a sibling ``<div class="single-essay__extra-reading">``
    which we also ignore
"""

from __future__ import annotations

import json
import pathlib
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

SOURCE_ID = "carlsmith"
RSS_URL = "https://joecarlsmith.com/rss.xml"


def _parse_rss(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    items = []
    for item in root.iter("item"):
        items.append(
            {
                "title": (item.findtext("title") or "").strip(),
                "link": (item.findtext("link") or "").strip(),
                "guid": (item.findtext("guid") or "").strip(),
                "pub_date": (item.findtext("pubDate") or "").strip(),
            }
        )
    return items


def _parse_body_html(html: str) -> tuple[list[Block], int]:
    soup = bs4.BeautifulSoup(html, "lxml")
    body = soup.find("div", class_="single-essay__main")
    if body is None:
        return [], 0

    # Strip inline footnote markers.
    for sup in body.find_all("sup", class_="article-reference"):
        sup.decompose()
    for sup in body.find_all("sup"):
        sup.decompose()

    # Count images before stripping. Prefer <figure> as the unit of meaning
    # (captioned image); count bare <img>s only when not wrapped in a figure.
    image_count = len(body.find_all("figure"))
    image_count += sum(1 for img in body.find_all("img") if not img.find_parent("figure"))

    # Drop images and figures — we don't surface them.
    for tag in body.find_all(("figure", "img")):
        tag.decompose()

    blocks: list[Block] = []
    seen: set[int] = set()
    for el in body.descendants:
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
    # URLs look like .../YYYY/MM/DD/<slug>
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

        if not html_path.exists():
            r = client.get(item["link"])
            r.raise_for_status()
            html_path.write_text(r.text)
        html = html_path.read_text()
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
            author="Joe Carlsmith",  # RSS omits dc:creator
            pub_date=item["pub_date"],
            blocks=blocks,
            markdown=markdown,
            image_count=image_count,
        )
        with open(json_path, "w") as f:
            json.dump(essay.to_json(), f, indent=2)
        essays.append(essay)
    return essays
