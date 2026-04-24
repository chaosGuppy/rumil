"""Regression tests for versus/prepare.py — the prefix_config_hash seed.

`prefix_config_hash` is the dedup axis for completions + judgments. Any
input that affects what the completion model sees must fork this hash
on re-import; otherwise cached rows silently render against the new
essay text while keying as if nothing changed.
"""

from __future__ import annotations

import sys
from pathlib import Path

_VERSUS_SRC = Path(__file__).resolve().parents[1] / "versus" / "src"
if str(_VERSUS_SRC) not in sys.path:
    sys.path.insert(0, str(_VERSUS_SRC))

from versus.essay import Block, Essay  # noqa: E402
from versus.prepare import prepare  # noqa: E402


def _essay(title: str = "An Essay", blocks: list[Block] | None = None) -> Essay:
    return Essay(
        id="src__slug",
        source_id="src",
        url="http://example.com",
        title=title,
        author="",
        pub_date="",
        blocks=blocks
        or [
            Block(type="p", text="Opening paragraph."),
            Block(type="p", text="Second paragraph continues."),
            Block(type="p", text="Third paragraph wraps up."),
        ],
        markdown="",
        image_count=0,
        schema_version=1,
    )


def _hash(essay: Essay) -> str:
    return prepare(
        essay, n_paragraphs=1, include_headers=False, length_tolerance=0.2
    ).prefix_config_hash


def test_title_change_forks_prefix_config_hash():
    a = _essay(title="Original Title")
    b = _essay(title="Edited Title")
    assert _hash(a) != _hash(b)


def test_block_change_forks_prefix_config_hash():
    blocks_a = [
        Block(type="p", text="One."),
        Block(type="p", text="Two."),
        Block(type="p", text="Three."),
    ]
    blocks_b = [
        Block(type="p", text="One."),
        Block(type="p", text="Two rewritten."),
        Block(type="p", text="Three."),
    ]
    assert _hash(_essay(blocks=blocks_a)) != _hash(_essay(blocks=blocks_b))


def test_identical_essay_is_stable():
    assert _hash(_essay()) == _hash(_essay())
