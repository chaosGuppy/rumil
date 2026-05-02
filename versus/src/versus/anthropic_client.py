"""Thin Anthropic Messages API client.

Mirrors openrouter.py in shape: a `chat()` function returning the full
response JSON plus an `extract_text()` helper. No SDK dependency -- we
talk to the REST endpoint directly so versus's dep surface stays small.

Used by rumil-style judge runs (where rumil deliberately calls Anthropic
directly rather than routing through OpenRouter). ANTHROPIC_API_KEY is
read from os.environ; callers are expected to apply the env cascade
(see envcascade.py) before invoking this module.
"""

from __future__ import annotations

import os
import time

import httpx

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"


def _headers() -> dict[str, str]:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY not set. Looked in versus/.env, "
            "<rumil-root>/.env, and the process environment."
        )
    return {
        "x-api-key": key,
        "anthropic-version": API_VERSION,
        "content-type": "application/json",
    }


def chat(
    model: str,
    messages: list[dict],
    temperature: float | None = None,
    max_tokens: int = 8000,
    top_p: float | None = None,
    timeout: float = 600.0,
    client: httpx.Client | None = None,
    retries: int = 2,
    system: str | None = None,
    thinking: dict | None = None,
    output_config: dict | None = None,
) -> dict:
    """Return full response JSON. Retries on transient empty-text failures.

    ``thinking`` is the Anthropic adaptive/extended-thinking dict (e.g.
    ``{"type": "adaptive"}``) or None. ``output_config`` carries
    ``{"effort": "..."}`` when applicable. Both default to None — pass
    them explicitly when the versus model registry says the model
    should run with them.
    """
    payload: dict = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    if temperature is not None:
        payload["temperature"] = temperature
    if top_p is not None:
        payload["top_p"] = top_p
    if system is not None:
        payload["system"] = system
    if thinking is not None:
        payload["thinking"] = thinking
    if output_config is not None:
        payload["output_config"] = output_config

    close = client is None
    client = client or httpx.Client(timeout=timeout)
    try:
        last_resp: dict = {}
        for attempt in range(retries + 1):
            r = client.post(API_URL, headers=_headers(), json=payload)
            r.raise_for_status()
            last_resp = r.json()
            text = _maybe_extract_text(last_resp)
            if text:
                return last_resp
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
        return last_resp
    finally:
        if close:
            client.close()


def _maybe_extract_text(resp: dict) -> str:
    blocks = resp.get("content") or []
    parts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
    return "".join(parts)


def extract_text(resp: dict) -> str:
    text = _maybe_extract_text(resp)
    if not text:
        stop = resp.get("stop_reason")
        usage = resp.get("usage", {})
        raise RuntimeError(
            f"empty text response (stop_reason={stop!r}, "
            f"output_tokens={usage.get('output_tokens')})"
        )
    return text
