"""Fetch forethought.org essays via RSS, cache HTML + parsed structure.

Parsed output is normalized to clean markdown. The import pipeline also strips:
  * the Footnotes section (heading + everything after)
  * Acknowledgements section if any
  * a trailing acknowledgement-like paragraph ("Thanks to ...", "This article has
    gone through several rounds of development...", "We would like to thank ...")
Images are not in our parse set, so no image-caption handling is needed.
"""

from __future__ import annotations

import json
import pathlib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict

import bs4
import httpx

RSS_URL = "https://www.forethought.org/feed"

# Version tag for parsed-essay cache. Bump when this module's output format changes.
SCHEMA_VERSION = 4

HEADING_COMPONENTS = {
    "Markdown-article-h1": "h1",
    "Markdown-article-h2": "h2",
    "Markdown-article-h3": "h3",
}
LIST_COMPONENTS = {"Markdown-ul", "Markdown-ol"}
PARA_COMPONENTS = {"Markdown-p"}
QUOTE_COMPONENTS = {"Markdown-blockquote"}
SUP_COMPONENTS = {"Markdown-sup"}

ACK_PATTERNS = [
    re.compile(r"^\s*thanks to\b", re.IGNORECASE),
    re.compile(r"^\s*(many\s+)?thanks,?\s", re.IGNORECASE),
    re.compile(r"^\s*we (would like|want) to (thank|acknowledge)\b", re.IGNORECASE),
    re.compile(r"^\s*special thanks to\b", re.IGNORECASE),
    re.compile(r"^\s*acknowledge?ments?\b", re.IGNORECASE),
    re.compile(
        r"^\s*this (article|series|post|piece|report) has gone through several rounds",
        re.IGNORECASE,
    ),
]
FOOTNOTES_HEADING_RE = re.compile(r"^\s*footnotes?\s*$", re.IGNORECASE)
ACK_HEADING_RE = re.compile(r"^\s*acknowledge?ments?\s*$", re.IGNORECASE)


@dataclass
class Block:
    type: str  # h1 | h2 | h3 | p  (p covers paragraphs, lists, blockquotes — text is pre-rendered md)
    text: str


@dataclass
class Essay:
    id: str
    url: str
    title: str
    author: str
    pub_date: str
    blocks: list[Block]
    markdown: str              # full body rendered as clean markdown (title NOT included)
    schema_version: int = SCHEMA_VERSION

    def to_json(self) -> dict:
        return asdict(self)


def _clean(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    # Collapse whitespace inserted before closing punctuation (artifact of
    # ``get_text(" ")`` putting a separator between a closing inline tag and
    # the trailing comma/period/etc).
    s = re.sub(r"\s+([,.;:!?\)\]])", r"\1", s)
    s = re.sub(r"([\(\[])\s+", r"\1", s)
    # Collapse the same artifact when a markdown emphasis marker sits between
    # text and punctuation (e.g. ``**Bold** :`` -> ``**Bold**:``).
    s = re.sub(r"(\*+)\s+([,.;:!?\)\]])", r"\1\2", s)
    return s


def _render_inline(node: bs4.PageElement) -> str:
    """Render a node's text content with bold/italic preserved as markdown.

    Skips nested ``Markdown-ul``/``Markdown-ol`` subtrees so list rendering can
    handle them separately. Footnote ``Markdown-sup`` markers are expected to
    have been removed beforehand.
    """
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
    """Return li's whose nearest Markdown-ul/ol ancestor is ``list_el``."""
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
    """Return Markdown-ul/ol's whose nearest Markdown-li ancestor is ``li``."""
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
    """A Markdown-p is treated as an image caption if every non-whitespace
    text node inside it is a descendant of an ``<em>`` tag.

    Matches the pattern used by forethought for image attribution lines —
    they sit beside a ``Markdown-img`` block and consist entirely of
    italicized text (often wrapped in a link to the source image).
    """
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


def parse_rss(xml_text: str) -> list[dict]:
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


def parse_article_html(html: str) -> list[Block]:
    soup = bs4.BeautifulSoup(html, "lxml")
    # Strip footnote markers up front; their bodies live in a separate
    # ``Footnotes`` section that ``clean_blocks`` drops, so any reference
    # left in the body would dangle as a bare digit.
    for sup in soup.find_all(attrs={"data-component": list(SUP_COMPONENTS)}):
        sup.decompose()
    containers = soup.find_all(attrs={"data-component": "Markdown"})
    if not containers:
        return []
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
                # Skip paragraph wrappers whose only real content is an image
                # (forethought renders figures inside Markdown-p with an sr-only
                # "Image" label; we don't want those as text blocks).
                if el.find(attrs={"data-component": "Markdown-img"}) is not None:
                    _mark_subtree(el, seen)
                    continue
                # Skip image attribution captions (fully-italicized paragraphs
                # adjacent to a Markdown-img block).
                if _is_caption_only_para(el):
                    _mark_subtree(el, seen)
                    continue
                text = _render_text(el)
                if text and text.lower() != "image":
                    blocks.append(Block(type="p", text=text))
                _mark_subtree(el, seen)
    return blocks


def _mark_subtree(el: bs4.element.Tag, seen: set[int]) -> None:
    for child in el.descendants:
        if isinstance(child, bs4.element.Tag):
            seen.add(id(child))
    seen.add(id(el))


def _looks_like_ack(text: str) -> bool:
    return any(p.match(text) for p in ACK_PATTERNS)


def clean_blocks(blocks: list[Block]) -> list[Block]:
    """Strip trailing footnotes/acknowledgements from parsed blocks.

    Walks from the end backwards. Removes:
      * Any heading that matches /^footnotes?$/i and everything after it.
      * Any heading that matches /^acknowledgements?$/i and everything after it.
      * Any trailing paragraph matching acknowledgement patterns.
    """
    # 1) Drop Footnotes section and anything after.
    cut = len(blocks)
    for i, b in enumerate(blocks):
        if b.type in ("h1", "h2", "h3") and FOOTNOTES_HEADING_RE.match(b.text):
            cut = i
            break
    blocks = blocks[:cut]

    # 2) Drop Acknowledgements section and anything after.
    cut = len(blocks)
    for i, b in enumerate(blocks):
        if b.type in ("h1", "h2", "h3") and ACK_HEADING_RE.match(b.text):
            cut = i
            break
    blocks = blocks[:cut]

    # 3) Trim trailing paragraphs matching ack patterns.
    while blocks and blocks[-1].type == "p" and _looks_like_ack(blocks[-1].text):
        blocks = blocks[:-1]

    return blocks


def blocks_to_markdown(blocks: list[Block]) -> str:
    """Render blocks as standard markdown body (no front-matter, no essay title)."""
    out: list[str] = []
    for b in blocks:
        if b.type == "h1":
            out.append(f"## {b.text}")
        elif b.type == "h2":
            out.append(f"### {b.text}")
        elif b.type == "h3":
            out.append(f"#### {b.text}")
        else:
            out.append(b.text)
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def fetch(
    cache_dir: pathlib.Path,
    raw_html_dir: pathlib.Path,
    max_recent: int,
    client: httpx.Client | None = None,
) -> list[Essay]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    raw_html_dir.mkdir(parents=True, exist_ok=True)
    close = client is None
    client = client or httpx.Client(
        follow_redirects=True,
        headers={"User-Agent": "versus-eval/0.0.1"},
        timeout=30.0,
    )
    try:
        rss = client.get(RSS_URL)
        rss.raise_for_status()
        items = parse_rss(rss.text)[:max_recent]

        essays: list[Essay] = []
        for item in items:
            essay_id = item["guid"] or _slug(item["link"])
            json_path = cache_dir / f"{essay_id}.json"
            html_path = raw_html_dir / f"{essay_id}.html"

            cached_ok = False
            if json_path.exists():
                with open(json_path) as f:
                    d = json.load(f)
                if d.get("schema_version") == SCHEMA_VERSION:
                    essays.append(
                        Essay(
                            id=d["id"],
                            url=d["url"],
                            title=d["title"],
                            author=d["author"],
                            pub_date=d["pub_date"],
                            blocks=[Block(**b) for b in d["blocks"]],
                            markdown=d["markdown"],
                            schema_version=d["schema_version"],
                        )
                    )
                    cached_ok = True
            if cached_ok:
                continue

            if not html_path.exists():
                r = client.get(item["link"])
                r.raise_for_status()
                html_path.write_text(r.text)
            html = html_path.read_text()
            raw_blocks = parse_article_html(html)
            if not raw_blocks:
                print(f"[warn] no blocks parsed for {essay_id}")
                continue
            blocks = clean_blocks(raw_blocks)
            markdown = blocks_to_markdown(blocks)

            essay = Essay(
                id=essay_id,
                url=item["link"],
                title=item["title"],
                author=item["author"],
                pub_date=item["pub_date"],
                blocks=blocks,
                markdown=markdown,
            )
            with open(json_path, "w") as f:
                json.dump(essay.to_json(), f, indent=2)
            essays.append(essay)
        return essays
    finally:
        if close:
            client.close()


def _slug(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]
