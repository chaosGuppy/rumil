"""Tests for ArtifactStore + SubroutineBase.render_artifact_block.

The k,v store is the channel for caller-seeded inputs (pair_text,
rubric, …) and spawn-produced outputs to flow through a SimpleSpine
run without mainline having to forward content. Tests pin the
collision/dedup contracts and the rendering format that subroutines
will see.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from rumil.orchestrators.simple_spine.artifacts import ArtifactStore
from rumil.orchestrators.simple_spine.subroutines.base import SpawnCtx, SubroutineBase


@dataclass(frozen=True, kw_only=True)
class _BareSub(SubroutineBase):
    """Minimal SubroutineBase concretization for testing render_artifact_block."""


def _bare_ctx(store: ArtifactStore | None, include: tuple[str, ...] = ()) -> SpawnCtx:
    return SpawnCtx(
        db=None,  # type: ignore[arg-type]
        budget_clock=None,  # type: ignore[arg-type]
        broadcaster=None,
        parent_call_id="c-1",
        question_id="q-1",
        spawn_id="s-1",
        artifacts=store,
        include_artifacts=include,
    )


def test_artifact_store_seeds_from_mapping():
    store = ArtifactStore(seed={"a": "alpha", "b": "beta"})
    assert store.list_keys() == ["a", "b"]
    art_a = store.get("a")
    assert art_a is not None
    assert art_a.text == "alpha"
    assert art_a.produced_by == "input"


def test_artifact_store_add_records_provenance():
    store = ArtifactStore()
    store.add("foo", "bar", produced_by="pair_notes", spawn_id="abc12345", round_idx=2)
    art = store.get("foo")
    assert art is not None
    assert art.produced_by == "pair_notes"
    assert art.spawn_id == "abc12345"
    assert art.round_idx == 2


def test_artifact_store_collision_raises():
    store = ArtifactStore(seed={"a": "alpha"})
    with pytest.raises(ValueError, match="already exists"):
        store.add("a", "different", produced_by="spawn:foo")


def test_artifact_store_get_returns_none_for_missing():
    store = ArtifactStore()
    assert store.get("nope") is None


def test_artifact_store_contains():
    store = ArtifactStore(seed={"x": "y"})
    assert "x" in store
    assert "absent" not in store


def test_artifact_store_render_block_uses_xml_fences():
    """XML fences let artifact bodies contain arbitrary markdown — including
    H2/H3 headers — without colliding with the boundary marker. Pin this
    so a future "let's go back to ### markdown" change breaks here.
    """
    store = ArtifactStore(seed={"pair_text": "## Body header\nlines"})
    block = store.render_block(["pair_text"])
    assert '<artifact key="pair_text"' in block
    assert "</artifact>" in block
    # Body markdown survives untouched, including its own ## header.
    assert "## Body header" in block


def test_artifact_store_render_block_includes_metadata_in_open_tag():
    store = ArtifactStore(seed={"k": "hello world"})
    block = store.render_block(["k"])
    assert 'chars="11"' in block
    assert 'from="input"' in block


def test_artifact_store_render_block_skips_missing_keys_silently():
    store = ArtifactStore(seed={"a": "alpha"})
    block = store.render_block(["a", "absent", "also_absent"])
    assert "alpha" in block
    # No <artifact> tag for the missing keys — silent skip.
    assert block.count("<artifact ") == 1


def test_artifact_store_render_block_preserves_order():
    store = ArtifactStore(seed={"first": "1", "second": "2"})
    block = store.render_block(["second", "first"])
    assert block.index("second") < block.index("first")


def test_artifact_store_render_seed_block_only_input_keys():
    store = ArtifactStore(seed={"seeded": "from-input"})
    store.add("from_spawn", "produced", produced_by="pair_notes", spawn_id="aaa", round_idx=0)
    block = store.render_seed_block()
    assert "seeded" in block
    assert "from_spawn" not in block


def test_artifact_store_render_seed_block_empty_when_no_seed():
    store = ArtifactStore()
    store.add("only_spawn", "x", produced_by="pair_notes", spawn_id="aaa", round_idx=0)
    assert store.render_seed_block() == ""


def test_artifact_store_announce_input():
    store = ArtifactStore(seed={"k": "abcdef"})
    line = store.announce("k")
    assert "`k`" in line
    assert "6 chars" in line
    assert "from input" in line


def test_artifact_store_announce_spawn_provenance():
    store = ArtifactStore()
    store.add(
        "pair_notes/abc12345", "x" * 1234, produced_by="pair_notes", spawn_id="abc", round_idx=2
    )
    line = store.announce("pair_notes/abc12345")
    assert "`pair_notes/abc12345`" in line
    assert "1,234 chars" in line
    assert "spawn:pair_notes" in line
    assert "round 2" in line


def test_artifact_store_announce_seed_lists_input_keys_only():
    store = ArtifactStore(seed={"a": "alpha", "b": "beta"})
    store.add("c", "gamma", produced_by="pair_notes", spawn_id="aaa", round_idx=0)
    lines = store.announce_seed()
    assert len(lines) == 2
    assert any("`a`" in line for line in lines)
    assert any("`b`" in line for line in lines)
    assert not any("`c`" in line for line in lines)


def test_artifact_store_require_keys_returns_missing():
    store = ArtifactStore(seed={"a": "alpha"})
    assert store.require_keys(["a"]) == []
    assert store.require_keys(["a", "b", "c"]) == ["b", "c"]


def test_render_artifact_block_combines_consumes_and_include():
    store = ArtifactStore(
        seed={"pair_text": "<<pair>>", "rubric": "<<rubric>>", "extra": "<<extra>>"}
    )
    sub = _BareSub(name="x", description="d", consumes=("pair_text", "rubric"))
    ctx = _bare_ctx(store, include=("extra",))
    block = sub.render_artifact_block(ctx)
    assert "<<pair>>" in block
    assert "<<rubric>>" in block
    assert "<<extra>>" in block


def test_render_artifact_block_dedups_overlap_in_order():
    store = ArtifactStore(seed={"a": "alpha", "b": "beta"})
    sub = _BareSub(name="x", description="d", consumes=("a", "b"))
    ctx = _bare_ctx(store, include=("a",))
    block = sub.render_artifact_block(ctx)
    # Each artifact appears exactly once even though `a` is in both consumes and include_artifacts.
    assert block.count('key="a"') == 1
    assert block.count('key="b"') == 1
    # consumes order is preserved (a before b).
    assert block.index('key="a"') < block.index('key="b"')


def test_render_artifact_block_raises_on_missing_consumes_key():
    store = ArtifactStore(seed={"present": "x"})
    sub = _BareSub(name="x", description="d", consumes=("present", "missing"))
    ctx = _bare_ctx(store)
    with pytest.raises(ValueError, match="missing"):
        sub.render_artifact_block(ctx)


def test_render_artifact_block_raises_when_artifacts_none_but_consumes_set():
    sub = _BareSub(name="x", description="d", consumes=("foo",))
    ctx = _bare_ctx(None)
    with pytest.raises(ValueError, match=r"ctx\.artifacts is None"):
        sub.render_artifact_block(ctx)


def test_render_artifact_block_empty_when_no_consumes_and_no_include():
    store = ArtifactStore(seed={"a": "alpha"})
    sub = _BareSub(name="x", description="d", consumes=())
    ctx = _bare_ctx(store)
    assert sub.render_artifact_block(ctx) == ""


def test_render_artifact_block_empty_when_artifacts_none_and_no_consumes():
    sub = _BareSub(name="x", description="d", consumes=())
    ctx = _bare_ctx(None)
    assert sub.render_artifact_block(ctx) == ""
