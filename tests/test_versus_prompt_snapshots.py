"""Snapshot guard for ``prompts/versus-*.md``.

The versus judge-prompt files are inputs to ``compute_prompt_hash`` and
feed the ``:p<hash>`` suffix on every rumil-style judge_model. An edit
that doesn't also bump the corresponding version constant silently
orphans existing judgment rows AND leaves no breadcrumb in the dedup
key that the change happened.

This test pins a sha256 for each file. When a pin fails, the failure
message tells you which file changed and which version constant to
bump alongside the edit. Regenerate pins below by running:

    uv run python -c "
    import hashlib
    from rumil.prompts import PROMPTS_DIR
    for p in sorted(PROMPTS_DIR.glob('versus-*.md')):
        print(f'    \"{p.name}\": \"{hashlib.sha256(p.read_bytes()).hexdigest()[:16]}\",')
    "

and pasting the output into ``EXPECTED_HASHES`` below.
"""

from __future__ import annotations

import hashlib
import pathlib

import pytest

from rumil.prompts import PROMPTS_DIR as _PROMPTS_DIR

# Bump-guidance: which version constant to bump when each prompt is
# edited. Printed in the failure message so the fix is unambiguous.
_BUMP_GUIDANCE = {
    "versus-judge-shell.md": (
        "Edits to the judge-shell affect every rumil-style judge "
        "(ws/orch/text) AND the `text`/`anthropic:*` path via "
        "compute_judge_prompt_hash. The :p<hash> suffix should fork "
        "naturally via compute_prompt_hash, BUT if you're making a "
        "semantic change the hash doesn't capture (e.g. whitespace-only "
        "or load-bearing invariants), also bump BLIND_JUDGE_VERSION in "
        "`versus.versions` (affects rumil:ws/orch/text) and/or "
        "JUDGE_PROMPT_VERSION (affects anthropic:*/OpenRouter)."
    ),
    "versus-general-quality.md": (
        "Dimension body for `general_quality`. The :p<hash> suffix "
        "forks naturally via compute_prompt_hash. If semantics change "
        "in a way the hash doesn't capture, bump BLIND_JUDGE_VERSION."
    ),
    "versus-grounding.md": (
        "Dimension body for `grounding`. The :p<hash> suffix forks "
        "naturally via compute_prompt_hash. If semantics change in a "
        "way the hash doesn't capture, bump BLIND_JUDGE_VERSION."
    ),
}

EXPECTED_HASHES = {
    "versus-general-quality.md": "194009cfe986eccd",
    "versus-grounding.md": "4a944364a2afa1d9",
    "versus-judge-shell.md": "0ef2d71735151ad6",
}


def _sha16(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


@pytest.mark.parametrize("filename", sorted(EXPECTED_HASHES.keys()))
def test_versus_prompt_snapshot_matches_pin(filename: str) -> None:
    path = _PROMPTS_DIR / filename
    assert path.exists(), f"expected prompt file missing: {path}"
    actual = _sha16(path)
    expected = EXPECTED_HASHES[filename]
    if actual != expected:
        guidance = _BUMP_GUIDANCE.get(filename, "No specific bump guidance for this file.")
        pytest.fail(
            f"prompt snapshot mismatch for `prompts/{filename}`:\n"
            f"  expected: {expected}\n"
            f"  actual:   {actual}\n\n"
            f"{guidance}\n\n"
            f"If the edit is intentional, update EXPECTED_HASHES in "
            f"tests/test_versus_prompt_snapshots.py to the new hash "
            f"and bump the relevant version constant if applicable."
        )


def test_versus_prompt_snapshot_covers_all_files() -> None:
    on_disk = {p.name for p in _PROMPTS_DIR.glob("versus-*.md")}
    pinned = set(EXPECTED_HASHES.keys())
    missing = on_disk - pinned
    stale = pinned - on_disk
    assert not missing, (
        f"new versus-*.md file(s) not pinned in EXPECTED_HASHES: "
        f"{sorted(missing)}. Add them to "
        f"tests/test_versus_prompt_snapshots.py with their current sha."
    )
    assert not stale, (
        f"pinned file(s) no longer exist on disk: {sorted(stale)}. "
        f"Remove them from EXPECTED_HASHES."
    )
