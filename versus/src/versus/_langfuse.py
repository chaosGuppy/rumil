"""Optional Langfuse integration for versus.

Self-contained — no rumil import. When ``LANGFUSE_PUBLIC_KEY`` is unset (or
the ``langfuse`` package is missing), every helper here is a no-op so versus
can be installed and run standalone.

The SDK reads its own ``LANGFUSE_PUBLIC_KEY`` / ``LANGFUSE_SECRET_KEY`` /
``LANGFUSE_HOST`` env vars at first client construction; we don't manage
those — callers are expected to load them via the env cascade before
firing requests.
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Callable, Iterator
from typing import Any, TypeVar

log = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])

try:
    from langfuse import get_client as _get_client
    from langfuse import observe as _observe

    _HAS_LANGFUSE = True
except ImportError:
    _HAS_LANGFUSE = False

    def _observe(**_kwargs: Any) -> Callable[[F], F]:
        def decorator(fn: F) -> F:
            return fn

        return decorator

    def _get_client() -> Any:
        return None


def _noop_decorator(**_kwargs: Any) -> Callable[[F], F]:
    def decorator(fn: F) -> F:
        return fn

    return decorator


def _enabled() -> bool:
    return _HAS_LANGFUSE and bool(os.environ.get("LANGFUSE_PUBLIC_KEY"))


# Silence the langfuse SDK's per-process "auth error: no public_key" warning
# when keys are deliberately unset. Without this, every versus run prints the
# SDK's complaint at first client construction, plus a "no active span"
# context-error each time an @observe-decorated function fires. Mirrors the
# treatment in rumil.tracing.langfuse_client.get_langfuse.
if _HAS_LANGFUSE and not os.environ.get("LANGFUSE_PUBLIC_KEY"):
    logging.getLogger("langfuse").setLevel(logging.ERROR)


def observe(**kwargs: Any) -> Callable[[F], F]:
    """`@observe(...)` passthrough; no-op when langfuse isn't enabled.

    Disabled = package missing OR ``LANGFUSE_PUBLIC_KEY`` unset. The langfuse
    SDK's real ``@observe`` tries to create spans against a disabled client and
    logs auth + context errors per call site, so we shadow it with a real
    no-op decorator in that case.
    """
    if not _enabled():
        return _noop_decorator(**kwargs)
    return _observe(**kwargs)


def update_generation(**kwargs: Any) -> None:
    """Best-effort enrichment of the active generation span."""
    if not _enabled():
        return
    try:
        client = _get_client()
        if client is None:
            return
        client.update_current_generation(**kwargs)
    except Exception as exc:
        log.debug("Langfuse update_generation failed: %s", exc)


@contextlib.contextmanager
def phase_span(name: str) -> Iterator[None]:
    """Open a Langfuse span for the duration of a workflow phase. No-op when disabled."""
    if not _enabled():
        yield
        return
    try:
        client = _get_client()
        if client is None:
            yield
            return
        with client.start_as_current_observation(name=name, as_type="span"):
            yield
    except Exception as exc:
        log.debug("Langfuse phase_span failed: %s", exc)
        yield
