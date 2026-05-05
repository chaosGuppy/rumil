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

import logging
import os
import time

import httpx

from versus._langfuse import observe, update_generation

log = logging.getLogger(__name__)

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


@observe(as_type="generation", name="versus.anthropic.messages")
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
    system_cache: bool = False,
) -> dict:
    """Return full response JSON. Retries on transient empty-text failures.

    ``thinking`` is the Anthropic adaptive/extended-thinking dict (e.g.
    ``{"type": "adaptive"}``) or None. ``output_config`` carries
    ``{"effort": "..."}`` when applicable. Both default to None — pass
    them explicitly when the versus model registry says the model
    should run with them.

    ``system_cache=True`` wraps the system prompt as a list with
    ``cache_control={'type': 'ephemeral'}`` so identical system
    prompts across calls get cached (~90% input-cost reduction on hits,
    ~25% extra one-time creation cost). Default False because the
    cache write is wasted when the prompt doesn't repeat. Opt in for
    sweep-style call sites (e.g., blind judging) where the same
    rubric runs against many pairs.
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
        if system_cache:
            payload["system"] = [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        else:
            payload["system"] = system
    if thinking is not None:
        payload["thinking"] = thinking
    if output_config is not None:
        payload["output_config"] = output_config

    close = client is None
    client = client or httpx.Client(timeout=timeout)
    try:
        last_resp: dict = {}
        try:
            for attempt in range(retries + 1):
                r = client.post(API_URL, headers=_headers(), json=payload)
                r.raise_for_status()
                last_resp = r.json()
                text = _maybe_extract_text(last_resp)
                if text:
                    _enrich_anthropic_generation(model, messages, payload, last_resp)
                    return last_resp
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
            _enrich_anthropic_generation(model, messages, payload, last_resp)
            return last_resp
        except Exception:
            # Enrich the active langfuse generation with whatever request
            # context we have so the failed span isn't bare. ``@observe``
            # will layer ``level=ERROR`` + ``status_message`` on re-raise.
            _enrich_anthropic_generation(model, messages, payload, last_resp)
            raise
    finally:
        if close:
            client.close()


def _enrich_anthropic_generation(
    model: str, messages: list[dict], payload: dict, resp: dict
) -> None:
    try:
        usage = resp.get("usage") or {}
        params = {
            k: payload.get(k)
            for k in ("temperature", "top_p", "max_tokens", "thinking", "output_config")
            if payload.get(k) is not None
        }
        system = payload.get("system")
        if isinstance(system, list):
            system = " ".join(b.get("text", "") for b in system if isinstance(b, dict))
        chatml_input: list[dict] = (
            [{"role": "system", "content": system}, *messages] if system else list(messages)
        )
        update_generation(
            model=model,
            input=chatml_input,
            output=_maybe_extract_text(resp) or None,
            model_parameters=params or None,
            usage_details={
                "input": usage.get("input_tokens") or 0,
                "output": usage.get("output_tokens") or 0,
                "cache_creation_input": usage.get("cache_creation_input_tokens") or 0,
                "cache_read_input": usage.get("cache_read_input_tokens") or 0,
            },
            metadata={"stop_reason": resp.get("stop_reason")},
        )
    except Exception as exc:
        log.debug("Langfuse enrichment (versus.anthropic) failed: %s", exc)


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
