"""Shared HTML helpers for standard-tag source parsers.

Forethought uses React-component CSS classes for its markup and has its
own list/blockquote handling in ``forethought.py``. Sources that emit
vanilla HTML (Substack, WordPress-like) can reuse this module to avoid
duplicating the ``<ul>/<ol>/<blockquote>/<strong>/<em>`` rendering.
"""

from __future__ import annotations

import re

import bs4


def clean_text(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\s+([,.;:!?\)\]])", r"\1", s)
    s = re.sub(r"([\(\[])\s+", r"\1", s)
    s = re.sub(r"(\*+)\s+([,.;:!?\)\]])", r"\1\2", s)
    return s


def render_inline(
    node: bs4.PageElement, *, skip_tag_names: frozenset[str] = frozenset({"ul", "ol"})
) -> str:
    """Render a node's text content with bold/italic preserved as markdown.

    Skips ``<ul>``/``<ol>`` subtrees so list rendering can handle them
    separately. Also skips ``<sup>`` descendants (commonly used for
    footnote markers — stripped up front before parsing).
    """
    if isinstance(node, bs4.NavigableString):
        return str(node)
    if not isinstance(node, bs4.element.Tag):
        return ""
    if node.name in skip_tag_names:
        return ""
    inner = "".join(render_inline(c, skip_tag_names=skip_tag_names) for c in node.children)
    if node.name in ("strong", "b"):
        stripped = inner.strip()
        return f" **{stripped}** " if stripped else ""
    if node.name in ("em", "i"):
        stripped = inner.strip()
        return f" *{stripped}* " if stripped else ""
    return inner


def render_text(el: bs4.element.Tag) -> str:
    return clean_text(render_inline(el))


def render_list(el: bs4.element.Tag, ordered: bool, depth: int = 0) -> str:
    """Render a standard-HTML ``<ul>``/``<ol>`` as nested markdown."""
    lines: list[str] = []
    indent = "  " * depth
    for i, li in enumerate(_direct_li_children(el), start=1):
        prefix = f"{i}. " if ordered else "- "
        text = render_text(li)
        if text:
            lines.append(indent + prefix + text)
        else:
            lines.append(indent + prefix.rstrip())
        for sub in _direct_sublists(li):
            sub_ordered = sub.name == "ol"
            sub_text = render_list(sub, ordered=sub_ordered, depth=depth + 1)
            if sub_text:
                lines.append(sub_text)
    return "\n".join(lines)


def _direct_li_children(list_el: bs4.element.Tag) -> list[bs4.element.Tag]:
    out: list[bs4.element.Tag] = []
    for li in list_el.find_all("li"):
        p = li.parent
        while p is not None:
            if isinstance(p, bs4.element.Tag) and p.name in ("ul", "ol"):
                if p is list_el:
                    out.append(li)
                break
            p = p.parent
    return out


def _direct_sublists(li: bs4.element.Tag) -> list[bs4.element.Tag]:
    out: list[bs4.element.Tag] = []
    for sub in li.find_all(("ul", "ol")):
        p = sub.parent
        while p is not None:
            if isinstance(p, bs4.element.Tag) and p.name == "li":
                if p is li:
                    out.append(sub)
                break
            p = p.parent
    return out


def render_blockquote(el: bs4.element.Tag) -> str:
    text = render_text(el)
    return "\n".join("> " + line for line in text.splitlines() or [text])


def is_caption_only_para(p: bs4.element.Tag) -> bool:
    """Return True if every non-whitespace text node in ``p`` is inside an ``<em>``.

    This catches image-caption paragraphs that would otherwise leak into the
    body after images have been decomposed (the pattern is: figure + sibling
    ``<p><em>Caption text</em></p>``).
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
