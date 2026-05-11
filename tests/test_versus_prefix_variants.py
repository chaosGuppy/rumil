"""Tests for prefix-variant resolution and config validation.

The variant model lets `versus/config.yaml` declare sibling prefix
configs alongside the canonical `prefix:` block. Run scripts and the
API select among them via `--prefix-label` / `?prefix_label=<id>`.
"""

import sys
from pathlib import Path

import pytest

_VERSUS_SRC = Path(__file__).resolve().parents[1] / "versus" / "src"
if str(_VERSUS_SRC) not in sys.path:
    sys.path.insert(0, str(_VERSUS_SRC))

import pydantic  # noqa: E402

from versus import config, prepare  # noqa: E402


def _make_cfg(prefix: dict, variants: list[dict] | None = None) -> config.Config:
    placeholder = config.VersusModelConfig(
        sampling=config.SamplingCfg(temperature=0, max_tokens=1024)
    )
    return config.Config(
        essays=config.EssaysCfg(),
        prefix=config.PrefixCfg(**prefix),
        prefix_variants=[config.PrefixCfg(**v) for v in (variants or [])],
        completion=config.CompletionCfg(models=[config.ModelCfg(id="m")]),
        judging=config.JudgingCfg(models=["j"]),
        models={"m": placeholder, "j": placeholder},
        storage=config.StorageCfg(),
    )


def test_canonical_prefix_defaults_to_default_id():
    cfg = _make_cfg(prefix={"n_paragraphs": 3, "include_headers": True})
    assert cfg.prefix.id == "default"
    assert prepare.active_prefix_configs(cfg) == [cfg.prefix]


def test_resolve_prefix_cfg_none_returns_canonical():
    cfg = _make_cfg(prefix={"n_paragraphs": 3, "include_headers": True})
    assert prepare.resolve_prefix_cfg(cfg, None) is cfg.prefix


def test_resolve_prefix_cfg_finds_variant():
    cfg = _make_cfg(
        prefix={"n_paragraphs": 3, "include_headers": True},
        variants=[{"id": "no_headers", "n_paragraphs": 3, "include_headers": False}],
    )
    resolved = prepare.resolve_prefix_cfg(cfg, "no_headers")
    assert resolved.id == "no_headers"
    assert resolved.include_headers is False


def test_resolve_prefix_cfg_finds_canonical_by_explicit_id():
    cfg = _make_cfg(
        prefix={"n_paragraphs": 3, "include_headers": True},
        variants=[{"id": "no_headers", "n_paragraphs": 3, "include_headers": False}],
    )
    resolved = prepare.resolve_prefix_cfg(cfg, "default")
    assert resolved is cfg.prefix


def test_resolve_prefix_cfg_unknown_label_raises():
    cfg = _make_cfg(prefix={"n_paragraphs": 3, "include_headers": True})
    with pytest.raises(ValueError, match="unknown prefix label"):
        prepare.resolve_prefix_cfg(cfg, "missing")


def test_active_prefix_configs_includes_canonical_first():
    cfg = _make_cfg(
        prefix={"id": "a", "n_paragraphs": 3, "include_headers": True},
        variants=[
            {"id": "b", "n_paragraphs": 3, "include_headers": False},
            {"id": "c", "n_paragraphs": 5, "include_headers": True},
        ],
    )
    assert [p.id for p in prepare.active_prefix_configs(cfg)] == ["a", "b", "c"]


def test_duplicate_prefix_ids_rejected():
    with pytest.raises(pydantic.ValidationError, match="duplicate prefix variant ids"):
        _make_cfg(
            prefix={"id": "x", "n_paragraphs": 3, "include_headers": True},
            variants=[{"id": "x", "n_paragraphs": 3, "include_headers": False}],
        )


def test_duplicate_default_id_rejected_when_variant_unnamed():
    with pytest.raises(pydantic.ValidationError, match="duplicate prefix variant ids"):
        _make_cfg(
            prefix={"n_paragraphs": 3, "include_headers": True},
            variants=[{"n_paragraphs": 3, "include_headers": False}],
        )


def test_variants_produce_distinct_prefix_config_hashes():
    """Sanity: flipping include_headers across variants forks the hash."""
    from versus.essay import Block, Essay

    essay = Essay(
        id="src__slug",
        source_id="src",
        url="",
        title="T",
        author="",
        pub_date="",
        blocks=[
            Block(type="p", text="One."),
            Block(type="p", text="Two."),
            Block(type="p", text="Three."),
            Block(type="h2", text="A section"),
            Block(type="p", text="Four."),
        ],
        markdown="",
        image_count=0,
        schema_version=1,
    )
    a = prepare.prepare(essay, n_paragraphs=3, include_headers=True, length_tolerance=0.1)
    b = prepare.prepare(essay, n_paragraphs=3, include_headers=False, length_tolerance=0.1)
    assert a.prefix_config_hash != b.prefix_config_hash
