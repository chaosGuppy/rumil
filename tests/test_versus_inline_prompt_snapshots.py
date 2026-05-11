"""Snapshot guard for versus prompt strings that live in python source.

Companion to ``test_versus_prompt_snapshots.py``, which covers the
``prompts/versus-*.md`` files. Two prompts on the completion/paraphrase
side are python inline strings rather than .md files:

- ``versus.paraphrase.PARAPHRASE_INSTRUCTIONS`` — the template passed to
  paraphrasing models; content forks ``sampling_hash`` only via
  ``PARAPHRASE_PROMPT_VERSION``, not via a prompt-file hash.
- ``versus.prepare.render_prompt`` — the completion prompt; its output
  feeds into ``prefix_config_hash`` via ``COMPLETION_PROMPT_VERSION``
  (the full content is not hashed directly).

For both, editing the inline string without bumping the matching
``*_PROMPT_VERSION`` silently leaves existing rows keyed as if the
prompt hadn't changed. This pin makes that mistake noisy instead of
silent: change the string, either update the pin AND bump the version,
or revert.
"""

import hashlib
import sys
from pathlib import Path

import pytest

_VERSUS_SRC = Path(__file__).resolve().parents[1] / "versus" / "src"
if str(_VERSUS_SRC) not in sys.path:
    sys.path.insert(0, str(_VERSUS_SRC))

from versus.essay import Block, Essay  # noqa: E402
from versus.paraphrase import PARAPHRASE_INSTRUCTIONS  # noqa: E402
from versus.prepare import prepare, render_prompt  # noqa: E402


def _sha16(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]


PARAPHRASE_INSTRUCTIONS_HASH = "09eee58a4a3cb6ae"
RENDER_PROMPT_SAMPLE_HASH = "e7005fe76da481da"


def _sample_task():
    essay = Essay(
        id="src__sample",
        source_id="src",
        url="http://example.com",
        title="Sample Essay",
        author="",
        pub_date="",
        blocks=[
            Block(type="p", text="Opening paragraph one."),
            Block(type="p", text="Paragraph two sets up the argument."),
            Block(type="p", text="Paragraph three is the pivot."),
            Block(type="h2", text="A section"),
            Block(type="p", text="Continuation paragraph after the split."),
        ],
        markdown="",
        image_count=0,
        schema_version=1,
    )
    return prepare(essay, n_paragraphs=2, include_headers=True, length_tolerance=0.2)


def test_paraphrase_instructions_pinned():
    actual = _sha16(PARAPHRASE_INSTRUCTIONS)
    if actual != PARAPHRASE_INSTRUCTIONS_HASH:
        pytest.fail(
            "PARAPHRASE_INSTRUCTIONS changed without updating the pin.\n"
            f"  expected: {PARAPHRASE_INSTRUCTIONS_HASH}\n"
            f"  actual:   {actual}\n\n"
            "If the edit is intentional:\n"
            "  1. Bump PARAPHRASE_PROMPT_VERSION in versus/versions.py "
            "(otherwise cached paraphrases keep the old sampling_hash "
            "and silently persist).\n"
            f"  2. Update PARAPHRASE_INSTRUCTIONS_HASH to {actual}."
        )


def test_render_prompt_pinned():
    task = _sample_task()
    rendered = render_prompt(task, include_headers=True, tolerance=0.2)
    actual = _sha16(rendered)
    if actual != RENDER_PROMPT_SAMPLE_HASH:
        pytest.fail(
            "render_prompt() output changed without updating the pin.\n"
            f"  expected: {RENDER_PROMPT_SAMPLE_HASH}\n"
            f"  actual:   {actual}\n\n"
            "If the edit is intentional:\n"
            "  1. Bump COMPLETION_PROMPT_VERSION in versus/versions.py "
            "(otherwise cached completions keep the old prefix_config_hash "
            "and silently persist against a new prompt).\n"
            f"  2. Update RENDER_PROMPT_SAMPLE_HASH to {actual}."
        )
