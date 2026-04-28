"""Source-agnostic essay data model and markdown rendering.

Every source fetcher (`versus.sources.*`) parses its own HTML into the
shared ``Block`` / ``Essay`` types defined here. Downstream modules
(`prepare`, `paraphrase`, `complete`, `status`, `versus_router`) only
depend on this module, not on any specific source.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

# Version tag for parsed-essay cache. Bump when Block / Essay shape
# changes in a way that should invalidate cached JSON.
SCHEMA_VERSION = 11


def is_current_schema(d: dict) -> bool:
    """True if a cached essay JSON's schema matches the live version."""
    return d.get("schema_version", 0) == SCHEMA_VERSION


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
    type: (
        str  # h1 | h2 | h3 | p  (p covers paragraphs, lists, blockquotes — text is pre-rendered md)
    )
    text: str


@dataclass
class Essay:
    id: str  # namespaced: "<source_id>__<slug>"
    source_id: str  # e.g. "forethought", "redwood", "carlsmith"
    url: str
    title: str
    author: str
    pub_date: str
    blocks: list[Block]
    markdown: str  # full body rendered as clean markdown (title NOT included)
    image_count: int = (
        0  # count of <img>/<figure>/image-component tags in the source HTML, pre-strip
    )
    schema_version: int = SCHEMA_VERSION

    def to_json(self) -> dict:
        return asdict(self)

    def paragraph_count(self) -> int:
        return sum(1 for b in self.blocks if b.type == "p")

    def image_ratio(self) -> float:
        """Images per paragraph. Returns 0 when the essay has no paragraphs."""
        pc = self.paragraph_count()
        return self.image_count / pc if pc else 0.0


def _looks_like_ack(text: str) -> bool:
    return any(p.match(text) for p in ACK_PATTERNS)


def clean_blocks(blocks: list[Block]) -> list[Block]:
    """Strip trailing footnotes/acknowledgements from parsed blocks.

    Walks from the end backwards. Removes:
      * Any heading that matches /^footnotes?$/i and everything after it.
      * Any heading that matches /^acknowledgements?$/i and everything after it.
      * Any trailing paragraph matching acknowledgement patterns.
    """
    cut = len(blocks)
    for i, b in enumerate(blocks):
        if b.type in ("h1", "h2", "h3") and FOOTNOTES_HEADING_RE.match(b.text):
            cut = i
            break
    blocks = blocks[:cut]

    cut = len(blocks)
    for i, b in enumerate(blocks):
        if b.type in ("h1", "h2", "h3") and ACK_HEADING_RE.match(b.text):
            cut = i
            break
    blocks = blocks[:cut]

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


def namespaced_id(source_id: str, slug: str) -> str:
    """Build a globally-unique essay id across sources.

    ``__`` is used as the separator: safe in filenames, URL path segments,
    and jsonl values. Callers should pass a ``slug`` free of ``__``.
    """
    return f"{source_id}__{slug}"


def clean_slug(s: str) -> str:
    """Slugify a URL path component. Lowercase, collapse non-alnum to single '-'."""
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s
