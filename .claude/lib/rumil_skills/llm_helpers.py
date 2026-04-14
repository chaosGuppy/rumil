"""Thin wrapper around rumil.llm.structured_call for skill meta-calls.

Meta-calls are LLM calls the skills make *about* rumil (e.g.
confusion scanning of a trace) rather than *as part of* a rumil
research call. Keeping them separate means:

- They don't register rows in ``runs`` / ``calls`` tables (meta-work
  shouldn't pollute research stats).
- They can use a different model than the research runs (cheaper
  default, override per-call).
- They reuse rumil's retry + cache infrastructure so prompt caching
  works across many scans with the same shared system prompt.

The big shared system prompt (`prompts/confusion_scan_system.md`) is
designed to be large and stable so successive scans get cache hits.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from rumil.llm import StructuredCallResult, structured_call
from rumil.settings import get_settings

PROMPTS_DIR = Path(__file__).parent / "prompts"

DEFAULT_META_MODEL = "claude-opus-4-6"


def load_prompt(name: str) -> str:
    """Load a shared system prompt file from the prompts/ dir."""
    path = PROMPTS_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"meta-call prompt not found: {path}")
    return path.read_text()


async def meta_structured_call[T: BaseModel](
    system_prompt: str,
    user_message: str,
    response_model: type[T],
    *,
    model: str | None = None,
) -> StructuredCallResult[T]:
    """Run a meta-LLM call with structured output, caching enabled.

    Uses rumil.llm.structured_call with:
      - ``cache=True`` so the big static system prompt is cached across
        repeat invocations (crucial for skills that scan many traces).
      - ``metadata=None`` / ``db=None`` so nothing is persisted to the
        ``calls`` / ``llm_exchanges`` tables.
      - Model override via ``model`` kwarg (defaults to DEFAULT_META_MODEL,
        which is cheaper than settings.model).
    """
    return await structured_call(
        system_prompt=system_prompt,
        user_message=user_message,
        response_model=response_model,
        cache=True,
        model=model or DEFAULT_META_MODEL,
    )


def resolved_meta_model(override: str | None = None) -> str:
    """Return the meta-call model to use, honoring the override chain."""
    if override:
        return override
    # Allow settings-level override without touching rumil_skills code.
    settings = get_settings()
    raw = getattr(settings, "meta_model", "") or ""
    if isinstance(raw, str) and raw:
        return raw
    return DEFAULT_META_MODEL
