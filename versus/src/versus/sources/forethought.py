"""Fetch forethought.org essays via RSS, cache HTML + parsed structure.

Forethought renders body text with React-component CSS classes
(``Markdown-article-h1``, ``Markdown-p``, ``Markdown-ul``, etc.) rather
than plain HTML tags, so the parser walks on ``data-component`` attrs.

The import pipeline strips:
  * the Footnotes section (heading + everything after) — handled by
    ``essay.clean_blocks``
  * Acknowledgements section if any — handled by ``essay.clean_blocks``
  * a trailing acknowledgement-like paragraph — handled by ``essay.clean_blocks``
Images are not in our parse set, so no image-caption handling is needed.
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

SOURCE_ID = "forethought"
RSS_URL = "https://www.forethought.org/feed"

HEADING_COMPONENTS = {
    "Markdown-article-h1": "h1",
    "Markdown-article-h2": "h2",
    "Markdown-article-h3": "h3",
}
LIST_COMPONENTS = {"Markdown-ul", "Markdown-ol"}
PARA_COMPONENTS = {"Markdown-p"}
QUOTE_COMPONENTS = {"Markdown-blockquote"}
SUP_COMPONENTS = {"Markdown-sup"}


def _clean(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\s+([,.;:!?\)\]])", r"\1", s)
    s = re.sub(r"([\(\[])\s+", r"\1", s)
    s = re.sub(r"(\*+)\s+([,.;:!?\)\]])", r"\1\2", s)
    return s


def _render_inline(node: bs4.PageElement) -> str:
    if isinstance(node, bs4.NavigableString):
        return str(node)
    if not isinstance(node, bs4.element.Tag):
        return ""
    if node.get("data-component") in LIST_COMPONENTS:
        return ""
    inner = "".join(_render_inline(c) for c in node.children)
    if node.name in ("strong", "b"):
        stripped = inner.strip()
        return f" **{stripped}** " if stripped else ""
    if node.name in ("em", "i"):
        stripped = inner.strip()
        return f" *{stripped}* " if stripped else ""
    return inner


def _render_text(el: bs4.element.Tag) -> str:
    return _clean(_render_inline(el))


def _direct_li_children(list_el: bs4.element.Tag) -> list[bs4.element.Tag]:
    out: list[bs4.element.Tag] = []
    for li in list_el.find_all(attrs={"data-component": "Markdown-li"}):
        p = li.parent
        while p is not None:
            if isinstance(p, bs4.element.Tag) and p.get("data-component") in LIST_COMPONENTS:
                if p is list_el:
                    out.append(li)
                break
            p = p.parent
    return out


def _direct_sublists(li: bs4.element.Tag) -> list[bs4.element.Tag]:
    out: list[bs4.element.Tag] = []
    for sub in li.find_all(attrs={"data-component": list(LIST_COMPONENTS)}):
        p = sub.parent
        while p is not None:
            if isinstance(p, bs4.element.Tag) and p.get("data-component") == "Markdown-li":
                if p is li:
                    out.append(sub)
                break
            p = p.parent
    return out


def _is_caption_only_para(p: bs4.element.Tag) -> bool:
    """A Markdown-p is a caption if every text node sits inside an ``<em>``."""
    has_text = False
    for s in p.find_all(string=True):
        if not s.strip():
            continue
        has_text = True
        ancestor = s.parent
        in_em = False
        while ancestor is not None and ancestor is not p:
            if isinstance(ancestor, bs4.element.Tag) and ancestor.name in ("em", "i"):
                in_em = True
                break
            ancestor = ancestor.parent
        if not in_em:
            return False
    return has_text


def _parse_rss(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    ns = {"dc": "http://purl.org/dc/elements/1.1/"}
    items = []
    for item in root.iter("item"):
        items.append(
            {
                "title": (item.findtext("title") or "").strip(),
                "link": (item.findtext("link") or "").strip(),
                "guid": (item.findtext("guid") or "").strip(),
                "pub_date": (item.findtext("pubDate") or "").strip(),
                "author": (item.findtext("dc:creator", namespaces=ns) or "").strip(),
                "description": (item.findtext("description") or "").strip(),
            }
        )
    return items


def _render_list(el: bs4.element.Tag, ordered: bool, depth: int = 0) -> str:
    lines: list[str] = []
    indent = "  " * depth
    for i, li in enumerate(_direct_li_children(el), start=1):
        prefix = f"{i}. " if ordered else "- "
        text = _render_text(li)
        if text:
            lines.append(indent + prefix + text)
        else:
            lines.append(indent + prefix.rstrip())
        for sub in _direct_sublists(li):
            sub_ordered = sub.get("data-component") == "Markdown-ol"
            sub_text = _render_list(sub, ordered=sub_ordered, depth=depth + 1)
            if sub_text:
                lines.append(sub_text)
    return "\n".join(lines)


def _render_blockquote(el: bs4.element.Tag) -> str:
    text = _render_text(el)
    return "\n".join("> " + line for line in text.splitlines() or [text])


def _parse_article_html(html: str) -> tuple[list[Block], int]:
    soup = bs4.BeautifulSoup(html, "lxml")
    for sup in soup.find_all(attrs={"data-component": list(SUP_COMPONENTS)}):
        sup.decompose()
    containers = soup.find_all(attrs={"data-component": "Markdown"})
    if not containers:
        return [], 0
    image_count = sum(
        len(c.find_all(attrs={"data-component": "Markdown-img"}))
        + len(c.find_all("img"))
        + len(c.find_all("figure"))
        for c in containers
    )
    blocks: list[Block] = []
    seen: set[int] = set()
    for container in containers:
        for el in container.descendants:
            if not isinstance(el, bs4.element.Tag):
                continue
            if id(el) in seen:
                continue
            comp = el.get("data-component")
            if not comp:
                continue
            if comp in HEADING_COMPONENTS:
                text = _render_text(el)
                if text:
                    blocks.append(Block(type=HEADING_COMPONENTS[comp], text=text))
                seen.add(id(el))
            elif comp in LIST_COMPONENTS:
                text = _render_list(el, ordered=(comp == "Markdown-ol"))
                if text:
                    blocks.append(Block(type="p", text=text))
                _mark_subtree(el, seen)
            elif comp in QUOTE_COMPONENTS:
                text = _render_blockquote(el)
                if text:
                    blocks.append(Block(type="p", text=text))
                _mark_subtree(el, seen)
            elif comp in PARA_COMPONENTS:
                if el.find(attrs={"data-component": "Markdown-img"}) is not None:
                    _mark_subtree(el, seen)
                    continue
                if _is_caption_only_para(el):
                    _mark_subtree(el, seen)
                    continue
                text = _render_text(el)
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
    return url.rstrip("/").rsplit("/", 1)[-1]


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
        raw_blocks, image_count = _parse_article_html(html)
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
