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
import functools
import logging
import os
import sys
from collections.abc import Callable, Iterator
from typing import Any, TypeVar, cast

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

# rumil.settings stores the host under ``langfuse_base_url`` and exports it
# to ``LANGFUSE_HOST`` only when get_langfuse() runs (which versus paths
# don't trigger). Mirror that mapping here so the SDK reads the right
# region — without this, US keys sent to the SDK's default host 401.
if _HAS_LANGFUSE and not os.environ.get("LANGFUSE_HOST"):
    base_url = os.environ.get("LANGFUSE_BASE_URL")
    if base_url:
        os.environ["LANGFUSE_HOST"] = base_url


def observe(**kwargs: Any) -> Callable[[F], F]:
    """`@observe(...)` passthrough that defers the enabled-check to call time.

    Disabled = package missing OR ``LANGFUSE_PUBLIC_KEY`` unset. The langfuse
    SDK's real ``@observe`` tries to create spans against a disabled client and
    logs auth + context errors per call site, so we shadow it with a plain
    pass-through call in that case.

    The check runs at *call* time, not decoration time. Modules that use
    ``@observe`` (``anthropic_client``, ``openrouter``) are imported by
    scripts before those scripts call ``envcascade.apply()`` to load
    ``LANGFUSE_PUBLIC_KEY`` from ``.env`` into the environment. A
    decoration-time check would lock in the disabled verdict for the
    whole process whenever the key lives only in ``.env``.
    """
    if not _HAS_LANGFUSE:
        return _noop_decorator(**kwargs)
    real_decorator = _observe(**kwargs)

    def decorator(fn: F) -> F:
        observed_fn = real_decorator(fn)

        @functools.wraps(fn)
        def wrapper(*args: Any, **kw: Any) -> Any:
            if _enabled():
                return observed_fn(*args, **kw)
            return fn(*args, **kw)

        return cast(F, wrapper)

    return decorator


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
    """Open a Langfuse span for the duration of a workflow phase. No-op when disabled.

    Telemetry must NEVER swallow application errors — SDK enter/exit failures
    are caught here, but exceptions raised inside the wrapped body propagate
    normally. Mirrors the pattern in ``rumil.tracing.langfuse_client.phase_span``.
    """
    if not _enabled():
        yield
        return
    span_cm = None
    try:
        client = _get_client()
        if client is not None:
            span_cm = client.start_as_current_observation(name=name, as_type="span")
            span_cm.__enter__()
    except Exception as exc:
        log.debug("Langfuse phase_span enter suppressed: %s", exc)
        span_cm = None
    try:
        yield
    finally:
        if span_cm is not None:
            try:
                span_cm.__exit__(*sys.exc_info())
            except Exception as exc:
                log.debug("Langfuse phase_span exit suppressed: %s", exc)
