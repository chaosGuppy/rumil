"""Langfuse Cloud integration helpers.

Single source of truth for talking to Langfuse from rumil. When
LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are set, every @observe-decorated
function emits spans to the configured project. When unset, the SDK initializes
in disabled mode and decorators become no-ops (one auth-error log line per
process — noise we silence below).
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Iterator
from functools import lru_cache

from langfuse import Langfuse, get_client

from rumil.settings import get_settings


@lru_cache(maxsize=1)
def get_langfuse() -> Langfuse | None:
    """Return a configured Langfuse client, or None when disabled.

    **Process-wide singleton.** The first caller's settings determine the
    enabled/disabled verdict and the client config (project keys, host) for
    the rest of the process. To switch keys mid-process — e.g. in tests
    that exercise both states — call ``get_langfuse.cache_clear()`` after
    swapping settings (the test suite does this via an autouse fixture).
    Even if you do clear our cache, ``langfuse.get_client()`` itself caches
    by public_key, so true mid-process key switching needs more care.

    The SDK reads its own LANGFUSE_PUBLIC_KEY/SECRET_KEY/HOST env vars at
    client construction; we copy from settings to those env vars here so
    each worktree's distinct .env feeds through correctly at process start.
    """
    settings = get_settings()
    if not settings.langfuse_enabled:
        # Silence the SDK's auth-error warning when we deliberately ran with
        # no keys — the user opted out, they don't need to see the complaint.
        logging.getLogger("langfuse").setLevel(logging.ERROR)
        return None
    os.environ["LANGFUSE_PUBLIC_KEY"] = settings.langfuse_public_key
    os.environ["LANGFUSE_SECRET_KEY"] = settings.langfuse_secret_key
    os.environ["LANGFUSE_HOST"] = settings.langfuse_base_url
    return get_client()


def langfuse_trace_url_for_current_observation() -> str | None:
    """Compose a deep-link URL to the current Langfuse trace + observation.

    Returns None when Langfuse is disabled or no observation is currently
    active. The URL points at the current observation inside its trace, so
    clicking it from a rumil LLM-exchange event lands on that exact LLM call
    in the Langfuse UI.
    """
    client = get_langfuse()
    if client is None:
        return None
    trace_id = client.get_current_trace_id()
    if not trace_id:
        return None
    base = client.get_trace_url(trace_id=trace_id)
    if not base:
        return None
    obs_id = client.get_current_observation_id()
    if obs_id:
        return f"{base}?observation={obs_id}"
    return base


def langfuse_trace_url_for_trace_id(trace_id: str) -> str | None:
    """Compose a Langfuse trace URL from an explicit trace_id."""
    client = get_langfuse()
    if client is None:
        return None
    return client.get_trace_url(trace_id=trace_id)


def flush_langfuse() -> None:
    """Best-effort flush of pending Langfuse events. Safe to call when disabled."""
    client = get_langfuse()
    if client is None:
        return
    client.flush()


@contextlib.contextmanager
def phase_span(name: str) -> Iterator[None]:
    """Open a Langfuse span for the duration of a workflow phase.

    No-op when Langfuse is disabled. Use this to add named spans without
    refactoring callers into their own decorated functions.
    """
    client = get_langfuse()
    if client is None:
        yield
        return
    with client.start_as_current_observation(name=name, as_type="span"):
        yield
