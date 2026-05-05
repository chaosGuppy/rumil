"""Thin OpenRouter client."""

from __future__ import annotations

import logging
import os

import httpx

from versus._langfuse import observe, update_generation

log = logging.getLogger(__name__)

API_URL = "https://openrouter.ai/api/v1/chat/completions"

# Per-model OpenRouter provider routing defaults. Used when the caller doesn't
# pass `provider=` explicitly. OpenAI direct is only sometimes listed for
# frontier models — for now we prefer OpenAI but allow fallback to Azure so
# requests don't 404 when OpenAI is unlisted.
PROVIDER_DEFAULTS: dict[str, dict] = {
    "openai/gpt-5.4": {"order": ["OpenAI"]},
}


def _headers() -> dict[str, str]:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/local/versus",
        "X-Title": "versus-eval",
    }


@observe(as_type="generation", name="versus.openrouter.chat")
def chat(
    model: str,
    messages: list[dict],
    temperature: float | None = None,
    max_tokens: int | None = None,
    top_p: float | None = None,
    timeout: float = 120.0,
    client: httpx.Client | None = None,
    retries: int = 2,
    provider: dict | None = None,
) -> dict:
    """Return full response JSON. Retries on transient null-content failures."""
    import time as _time

    payload: dict = {"model": model, "messages": messages}
    if temperature is not None:
        payload["temperature"] = temperature
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if top_p is not None:
        payload["top_p"] = top_p
    if provider is None:
        provider = PROVIDER_DEFAULTS.get(model)
    if provider is not None:
        payload["provider"] = provider

    close = client is None
    client = client or httpx.Client(timeout=timeout)
    try:
        last_resp: dict | None = None
        try:
            for attempt in range(retries + 1):
                r = client.post(API_URL, headers=_headers(), json=payload)
                r.raise_for_status()
                last_resp = r.json()
                content = last_resp["choices"][0]["message"].get("content")
                if content is not None and content != "":
                    _enrich_openrouter_generation(model, messages, payload, last_resp)
                    return last_resp
                # empty content: transient upstream failure — backoff + retry
                if attempt < retries:
                    _time.sleep(1.5 * (attempt + 1))
            if last_resp is not None:
                _enrich_openrouter_generation(model, messages, payload, last_resp)
            return last_resp  # caller's extract_text will raise a clean error
        except Exception:
            # Enrich the active langfuse generation with whatever request
            # context we have so the failed span isn't bare. ``@observe``
            # will layer ``level=ERROR`` + ``status_message`` on re-raise.
            _enrich_openrouter_generation(model, messages, payload, last_resp or {})
            raise
    finally:
        if close:
            client.close()


def _enrich_openrouter_generation(
    model: str, messages: list[dict], payload: dict, resp: dict
) -> None:
    try:
        usage = resp.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}
        choices = resp.get("choices") or []
        choice = choices[0] if choices else {}
        message = choice.get("message") or {}
        params = {
            k: payload.get(k)
            for k in ("temperature", "top_p", "max_tokens", "provider")
            if payload.get(k) is not None
        }
        cost = usage.get("cost")
        update_generation(
            model=model,
            input=messages,
            output=message.get("content"),
            model_parameters=params or None,
            usage_details={
                "input": usage.get("prompt_tokens") or 0,
                "output": usage.get("completion_tokens") or 0,
                "reasoning": details.get("reasoning_tokens") or 0,
            },
            cost_details=({"total": float(cost)} if isinstance(cost, (int, float)) else None),
            metadata={"finish_reason": choice.get("finish_reason")},
        )
    except Exception as exc:
        log.debug("Langfuse enrichment (versus.openrouter) failed: %s", exc)


def extract_text(resp: dict) -> str:
    choice = resp["choices"][0]
    content = choice["message"].get("content")
    if content is None:
        fr = choice.get("finish_reason")
        usage = resp.get("usage", {})
        raise RuntimeError(
            f"null content (finish_reason={fr!r}, "
            f"completion_tokens={usage.get('completion_tokens')}, "
            f"reasoning_tokens={(usage.get('completion_tokens_details') or {}).get('reasoning_tokens')})"
        )
    return content
