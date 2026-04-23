"""Split an essay into (prefix, remainder) for the completion task."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from versus.fetch import Block, Essay, blocks_to_markdown

# Bump when ``render_prompt`` text changes in a way that should invalidate
# existing completion rows. Folded into ``prefix_config_hash`` below so
# every downstream key (completions AND judgments keyed on prefix_hash)
# forks naturally. Edit the prompt without bumping this and old rows
# silently persist.
COMPLETION_PROMPT_VERSION = 2


@dataclass
class PreparedTask:
    essay_id: str
    title: str
    prefix_blocks: list[Block]
    remaining_headers: list[Block]
    prefix_markdown: str  # rendered md for the prefix (no title)
    remainder_markdown: str  # rendered md for the remainder (used as human baseline)
    target_words: int
    prefix_config_hash: str  # stable hash of (essay content, n_paragraphs, include_headers, length_tolerance, COMPLETION_PROMPT_VERSION)


def _word_count(text: str) -> int:
    return len(text.split())


def _content_hash(essay: Essay) -> str:
    payload = json.dumps(
        [{"type": b.type, "text": b.text} for b in essay.blocks],
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:10]


def prepare(
    essay: Essay,
    n_paragraphs: int,
    include_headers: bool,
    length_tolerance: float,
) -> PreparedTask:
    prefix_blocks: list[Block] = []
    remainder_blocks: list[Block] = []
    paragraphs_taken = 0
    for b in essay.blocks:
        if paragraphs_taken < n_paragraphs:
            prefix_blocks.append(b)
            if b.type == "p":
                paragraphs_taken += 1
        else:
            remainder_blocks.append(b)

    remaining_headers = [b for b in remainder_blocks if b.type in ("h1", "h2", "h3")]
    prefix_markdown = blocks_to_markdown(prefix_blocks).rstrip() + "\n"
    remainder_markdown = blocks_to_markdown(remainder_blocks).rstrip() + "\n"
    target_words = _word_count(" ".join(b.text for b in remainder_blocks if b.type == "p"))

    cfg_key = {
        "n_paragraphs": n_paragraphs,
        "include_headers": include_headers,
        "length_tolerance": length_tolerance,
        "content_hash": _content_hash(essay),
        "prompt_version": COMPLETION_PROMPT_VERSION,
    }
    prefix_config_hash = hashlib.sha256(
        (essay.id + "|" + json.dumps(cfg_key, sort_keys=True)).encode()
    ).hexdigest()[:16]

    return PreparedTask(
        essay_id=essay.id,
        title=essay.title,
        prefix_blocks=prefix_blocks,
        remaining_headers=remaining_headers,
        prefix_markdown=prefix_markdown,
        remainder_markdown=remainder_markdown,
        target_words=target_words,
        prefix_config_hash=prefix_config_hash,
    )


def split_paraphrase(
    paraphrase_markdown_blocks: list[Block],
    n_paragraphs: int,
) -> str:
    """Given a paraphrase's blocks, return its remainder markdown at the same split point."""
    prefix, remainder = [], []
    paragraphs_taken = 0
    for b in paraphrase_markdown_blocks:
        if paragraphs_taken < n_paragraphs:
            prefix.append(b)
            if b.type == "p":
                paragraphs_taken += 1
        else:
            remainder.append(b)
    return blocks_to_markdown(remainder).rstrip() + "\n"


def render_prompt(task: PreparedTask, include_headers: bool, tolerance: float) -> str:
    low = int(task.target_words * (1 - tolerance))
    high = int(task.target_words * (1 + tolerance))

    parts = [
        "You are continuing an essay from forethought.org. Below is the beginning;",
        "continue it in the same voice and style. Do not restate or summarize the opening —",
        f"write only the continuation. Aim for about {task.target_words} words",
        f"(between {low} and {high} is fine). Use Markdown section headings if it helps structure.",
    ]
    if include_headers and task.remaining_headers:
        parts.append("")
        parts.append("The remaining essay covers these sections in order:")
        for h in task.remaining_headers:
            indent = {"h1": "- ", "h2": "  - ", "h3": "    - "}[h.type]
            parts.append(f"{indent}{h.text}")
    parts.append("")
    parts.append("BEGIN ESSAY")
    parts.append("===")
    parts.append(f"# {task.title}")
    parts.append("")
    parts.append(task.prefix_markdown.rstrip())
    parts.append("===")
    parts.append("")
    parts.append("Continue from here:")
    return "\n".join(parts)
