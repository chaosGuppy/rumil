"""Split an essay into (prefix, remainder) for the completion task."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass

from versus.essay import SCHEMA_VERSION, Block, Essay, blocks_to_markdown
from versus.versions import COMPLETION_PROMPT_VERSION


@dataclass
class PreparedTask:
    essay_id: str
    source_id: str
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
    # Title is rendered into the completion prompt via `# {task.title}`
    # in render_prompt, so a title-only re-fetch changes what the model
    # sees and must fork prefix_config_hash. Blocks alone aren't enough.
    payload = json.dumps(
        {
            "title": essay.title,
            "blocks": [{"type": b.type, "text": b.text} for b in essay.blocks],
        },
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
        source_id=essay.source_id,
        title=essay.title,
        prefix_blocks=prefix_blocks,
        remaining_headers=remaining_headers,
        prefix_markdown=prefix_markdown,
        remainder_markdown=remainder_markdown,
        target_words=target_words,
        prefix_config_hash=prefix_config_hash,
    )


def active_prefix_configs(cfg) -> list:
    """Canonical prefix + sibling variants, in declaration order.

    Used by per-variant fan-out (status reporting, multi-variant API
    aggregations). Run scripts default to the canonical entry only and
    opt into a specific sibling via ``--prefix-label``.
    """
    return [cfg.prefix, *cfg.prefix_variants]


def resolve_prefix_cfg(cfg, label: str | None):
    """Return the prefix config with id ``label`` (None → canonical).

    Raises ValueError if no variant has the requested id, with the list
    of valid labels for callers to surface.
    """
    if label is None:
        return cfg.prefix
    for pcfg in active_prefix_configs(cfg):
        if pcfg.id == label:
            return pcfg
    valid = [p.id for p in active_prefix_configs(cfg)]
    raise ValueError(f"unknown prefix label {label!r}; valid: {valid}")


def current_prefix_hashes(cfg, essays_dir, *, prefix_cfg=None) -> dict[str, str]:
    """Return ``{essay_id: prefix_config_hash}`` for every cached essay.

    Loads each essay JSON in ``essays_dir`` (skipping ``.verdict.json``
    companion files) and runs :func:`prepare` to compute the live hash
    that's a function of essay content + prefix params + prompt version.
    Used by judging scripts with ``--current-only`` to skip groups whose
    prefix_hash is no longer current.

    ``prefix_cfg`` defaults to ``cfg.prefix``. Pass a sibling from
    ``cfg.prefix_variants`` to compute live hashes under that variant
    (the API uses this to scope staleness to a selected variant).
    """
    import json
    import pathlib

    pcfg = prefix_cfg if prefix_cfg is not None else cfg.prefix
    out: dict[str, str] = {}
    d = pathlib.Path(essays_dir)
    if not d.exists():
        return out
    for path in sorted(d.glob("*.json")):
        if path.name.endswith(".verdict.json"):
            continue
        data = json.loads(path.read_text())
        if "source_id" not in data:
            # Legacy (pre-multi-source) essay JSON — skip. Re-fetch to upgrade.
            continue
        essay = Essay(
            id=data["id"],
            source_id=data["source_id"],
            url=data.get("url", ""),
            title=data.get("title", ""),
            author=data.get("author", ""),
            pub_date=data.get("pub_date", ""),
            blocks=[Block(**b) for b in data["blocks"]],
            markdown=data.get("markdown", ""),
            image_count=data.get("image_count", 0),
            schema_version=data.get("schema_version", 0),
        )
        task = prepare(
            essay,
            n_paragraphs=pcfg.n_paragraphs,
            include_headers=pcfg.include_headers,
            length_tolerance=cfg.completion.length_tolerance,
        )
        out[essay.id] = task.prefix_config_hash
    return out


def active_essay_ids(essays_dir, exclude_ids: Iterable[str]) -> set[str]:
    """Essay IDs in the current canonical set.

    Applies the same gate as the API's ``_build_essays_status``:
    legacy pre-multi-source JSONs are skipped, essays at an older
    ``schema_version`` are skipped, and ``exclude_ids`` are skipped.
    Used by ``scripts/run_completions.py`` / ``run_judgments.py``
    ``--active`` so they touch exactly the essays ``/versus`` would
    enumerate.
    """
    import pathlib

    exclude = set(exclude_ids)
    out: set[str] = set()
    d = pathlib.Path(essays_dir)
    if not d.exists():
        return out
    for path in sorted(d.glob("*.json")):
        if path.name.endswith(".verdict.json"):
            continue
        data = json.loads(path.read_text())
        if "source_id" not in data:
            continue
        if data.get("schema_version") != SCHEMA_VERSION:
            continue
        if data["id"] in exclude:
            continue
        out.add(data["id"])
    return out


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
        "You are continuing an essay. Below is the beginning; write the best",
        "continuation you can — substantive, specific, engaged with the opening's",
        "topic. Don't restate the opening, hedge performatively, or drift generic.",
        f"Aim for about {task.target_words} words (between {low} and {high} is fine).",
        "Use Markdown section headings if it helps structure.",
        "",
        "You may use scratch space to think through your approach first — outline",
        "the argument, sketch sections, note dead ends. Wrap your final continuation",
        "in <continuation>...</continuation> tags; only the content inside those tags",
        "is evaluated.",
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
