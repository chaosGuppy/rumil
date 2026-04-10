"""Terse output helpers tuned for Claude Code's conversation surface.

Rules of thumb:
  - One line per significant event. No decorative banners.
  - Always surface the trace URL as early as possible so the user can open it
    in a browser alongside the CC session.
  - Short IDs (first 8 chars) for human scanning; full IDs only when needed
    for downstream commands.
"""

from __future__ import annotations

from rumil.settings import get_settings


def short(page_or_id: object) -> str:
    """First 8 chars of a page/call ID, from a string or any obj with an .id attr."""
    if isinstance(page_or_id, str):
        return page_or_id[:8]
    pid = getattr(page_or_id, "id", None)
    if isinstance(pid, str):
        return pid[:8]
    return str(page_or_id)[:8]


def trace_url(run_id: str) -> str:
    """Full frontend URL for a run's trace view."""
    frontend = get_settings().frontend_url
    return f"{frontend}/traces/{run_id}"


def print_trace(run_id: str, label: str = "trace") -> None:
    """Print the trace URL on its own line, prefixed with a label."""
    print(f"{label}: {trace_url(run_id)}", flush=True)


def print_event(symbol: str, message: str) -> None:
    """Print a single terse event line. Symbol is usually one char (→ ⚙ ✓ ✗ •)."""
    print(f"{symbol} {message}", flush=True)


def truncate(text: str, length: int = 80) -> str:
    if len(text) <= length:
        return text
    return text[: length - 1] + "…"
