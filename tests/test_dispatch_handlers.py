"""Registry-level tests for the dispatch handlers registry.

These tests guard the invariant that DISPATCH_HANDLERS in
orchestrators/dispatch_handlers.py covers every payload type that
prioritization can produce, so a new dispatchable call type cannot
silently fall through _execute_dispatch.
"""

from collections.abc import Awaitable, Callable

import pytest

from rumil.calls.dispatches import DISPATCH_DEFS
from rumil.models import BaseDispatchPayload, CreateViewDispatchPayload
from rumil.orchestrators.dispatch_handlers import (
    DISPATCH_HANDLERS,
    DispatchContext,
    DispatchHandler,
)


def test_registry_is_nonempty():
    assert len(DISPATCH_HANDLERS) > 0


def test_every_key_is_a_base_dispatch_payload_subclass():
    """Every registry key must be a concrete BaseDispatchPayload subclass."""
    for key in DISPATCH_HANDLERS:
        assert isinstance(key, type), f"Registry key {key!r} is not a type"
        assert issubclass(key, BaseDispatchPayload), (
            f"{key.__name__} is not a BaseDispatchPayload subclass"
        )


def test_every_value_is_callable():
    for payload_type, handler in DISPATCH_HANDLERS.items():
        assert callable(handler), (
            f"Handler for {payload_type.__name__} is not callable"
        )


def test_every_dispatch_def_payload_has_handler():
    """Every payload schema in DISPATCH_DEFS must have a handler.

    DISPATCH_DEFS is the set of dispatches that prioritization LLM tools
    can emit. If a payload type is in DISPATCH_DEFS but not in
    DISPATCH_HANDLERS, _execute_dispatch will silently return None when
    the LLM calls that tool — a bug with no immediate failure.
    """
    missing: list[str] = []
    for call_type, ddef in DISPATCH_DEFS.items():
        schema = ddef.schema
        if schema not in DISPATCH_HANDLERS:
            missing.append(f"{call_type.value} ({schema.__name__})")
    assert not missing, (
        "DISPATCH_DEFS payload types without a handler: " + ", ".join(missing)
    )


def test_internal_dispatch_types_also_have_handlers():
    """Dispatch payload types used internally (not via LLM tools) must also
    be covered. CreateViewDispatchPayload is created by orchestrator code
    directly, not via a DispatchDef, so the DISPATCH_DEFS check above
    doesn't catch it."""
    assert CreateViewDispatchPayload in DISPATCH_HANDLERS


def test_dispatch_handler_type_alias_is_consistent():
    """The DispatchHandler type alias should accept the actual handler shape."""
    # Sanity check: the type alias exists and is what we expect.
    # This is a structural assertion — if anyone changes DispatchHandler
    # without updating handlers, mypy/pyright will catch it but a unit
    # test adds a second layer of defence.
    assert DispatchHandler is not None
    # All registered handlers must be Callable[[DispatchContext, payload], Awaitable[...]]
    # We can't check the type parameters at runtime, but we can check the
    # callable arity via inspection.
    import inspect

    for payload_type, handler in DISPATCH_HANDLERS.items():
        sig = inspect.signature(handler)
        params = list(sig.parameters.values())
        assert len(params) == 2, (
            f"Handler for {payload_type.__name__} has {len(params)} parameters, expected 2"
        )


def test_dispatch_context_fields():
    """DispatchContext carries the fields _execute_dispatch needs to pass
    to handlers. This test pins the field set so accidental removals fail
    loudly."""
    # Build a context with a stand-in orchestrator. This is just a
    # structural smoke test — no behaviour is exercised.
    ctx = DispatchContext(
        orchestrator=None,  # type: ignore[arg-type]
        resolved_question_id="q1",
        parent_call_id="p1",
        force=False,
        call_id="c1",
        sequence_id="s1",
        sequence_position=3,
        d_label="label",
    )
    assert ctx.resolved_question_id == "q1"
    assert ctx.parent_call_id == "p1"
    assert ctx.force is False
    assert ctx.call_id == "c1"
    assert ctx.sequence_id == "s1"
    assert ctx.sequence_position == 3
    assert ctx.d_label == "label"
