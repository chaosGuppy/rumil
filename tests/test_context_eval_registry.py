"""Unit tests for the context-builder eval named-builder registry."""

import pytest

from rumil.calls.context_builder_eval import (
    EVAL_CONTEXT_BUILDERS,
    GOLD_CONTEXT_BUILDER,
    make_eval_context_builder,
)
from rumil.calls.context_builders import EmbeddingContext
from rumil.calls.impact_filtered_context import ImpactFilteredContext
from rumil.models import CallType


def test_embedding_context_factory_returns_embedding_context():
    builder = make_eval_context_builder("EmbeddingContext", CallType.CONTEXT_BUILDER_EVAL)
    assert isinstance(builder, EmbeddingContext)


def test_impact_filtered_context_factory_wraps_embedding_context():
    builder = make_eval_context_builder("ImpactFilteredContext", CallType.CONTEXT_BUILDER_EVAL)
    assert isinstance(builder, ImpactFilteredContext)
    inner = builder._inner  # type: ignore[attr-defined]
    assert isinstance(inner, EmbeddingContext)


def test_unknown_builder_name_raises_with_valid_names_listed():
    with pytest.raises(ValueError) as excinfo:
        make_eval_context_builder("Nonsense", CallType.CONTEXT_BUILDER_EVAL)
    msg = str(excinfo.value)
    assert "Nonsense" in msg
    for name in EVAL_CONTEXT_BUILDERS:
        assert name in msg


def test_gold_context_builder_constant_is_in_registry():
    assert GOLD_CONTEXT_BUILDER in EVAL_CONTEXT_BUILDERS
