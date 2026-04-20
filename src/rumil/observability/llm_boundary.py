"""Boundary log of every Anthropic API exchange.

Captures the full request payload and response object for every Anthropic
API call across the codebase — `llm.call_api`, `llm._structured_call_parse`,
`chat.handle_chat`, `chat.handle_chat_stream`, and any future caller using
`make_anthropic_client()`.

Staging note: this is transport-level observability — every row records a
real API exchange (bytes flowed, dollars spent), so all rows are visible
to all readers regardless of whether the originating run is staged. Use
the `run_id` column to scope to a particular (staged or not) run.

Run/call/db scope is propagated via ContextVars so the wrappers don't need
to thread arguments through every call site:

  - `current_db_var`: the DB instance to insert against. Set by
    `RunExecutor.tracked_scope` (via `set_run_context`).
  - `current_run_id_var`: the active run_id. Set by `tracked_scope`.
  - `current_call_id_var`: the active call_id when inside a `CallRunner`.
    Set by `CallRunner.run` (via `set_call_context`).

Failures to insert never crash the API call — they're logged to stderr.

The Anthropic API key is in the Authorization header (added by the SDK
transport), never in the request body. We log only the request kwargs
(`model`, `system`, `messages`, `tools`, `max_tokens`, `temperature`,
`thinking`, `output_config`, ...) — none of these contain auth.
"""

from __future__ import annotations

import json
import logging
from contextvars import ContextVar, Token
from datetime import datetime
from typing import TYPE_CHECKING, Any

from rumil.settings import get_settings

if TYPE_CHECKING:
    import anthropic.types

    from rumil.database import DB

log = logging.getLogger(__name__)


current_db_var: ContextVar[DB | None] = ContextVar("rumil_boundary_db", default=None)
current_run_id_var: ContextVar[str | None] = ContextVar("rumil_boundary_run_id", default=None)
current_call_id_var: ContextVar[str | None] = ContextVar("rumil_boundary_call_id", default=None)


def set_run_context(*, db: DB, run_id: str | None) -> tuple[Token, Token]:
    """Set DB + run_id contextvars. Returns reset tokens for use with reset_run_context."""
    return (current_db_var.set(db), current_run_id_var.set(run_id))


def reset_run_context(tokens: tuple[Token, Token]) -> None:
    db_tok, run_tok = tokens
    current_run_id_var.reset(run_tok)
    current_db_var.reset(db_tok)


def set_call_context(call_id: str | None) -> Token:
    return current_call_id_var.set(call_id)


def reset_call_context(token: Token) -> None:
    current_call_id_var.reset(token)


def _to_json_safe(value: Any) -> Any:
    """Recursively coerce to a JSON-serializable structure.

    Pydantic models and Anthropic SDK content blocks get .model_dump()'d;
    other unrecognized objects fall back to repr.
    """
    if isinstance(value, dict):
        return {str(k): _to_json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_json_safe(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump(mode="json")
        except Exception:
            return repr(value)
    return repr(value)


def _serialize_response(response: anthropic.types.Message | None) -> dict | None:
    if response is None:
        return None
    try:
        return response.model_dump(mode="json")
    except Exception:
        return {
            "id": getattr(response, "id", None),
            "model": getattr(response, "model", None),
            "stop_reason": getattr(response, "stop_reason", None),
        }


def _serialize_usage(response: anthropic.types.Message | None) -> dict | None:
    if response is None or not hasattr(response, "usage"):
        return None
    usage = response.usage
    return {
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
        "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", None),
        "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", None),
    }


async def log_exchange(
    *,
    source: str,
    model: str,
    request_payload: dict,
    started_at: datetime,
    finished_at: datetime,
    response: anthropic.types.Message | None = None,
    error: BaseException | None = None,
    streamed: bool = False,
) -> None:
    """Insert a row into llm_boundary_exchanges. Never raises.

    Reads run_id / call_id / db from contextvars. If no DB is in context
    (e.g. a code path that hasn't been wrapped with set_run_context yet),
    the call is silently dropped — log boundary is best-effort.
    """
    if not get_settings().log_llm_boundary_enabled:
        return
    db = current_db_var.get()
    if db is None:
        log.debug("llm-boundary: no DB in context (source=%s); skipping insert", source)
        return

    latency_ms = max(0, int((finished_at - started_at).total_seconds() * 1000))
    response_json = _serialize_response(response)
    usage = _serialize_usage(response)
    stop_reason = getattr(response, "stop_reason", None) if response is not None else None

    error_class: str | None = None
    error_message: str | None = None
    http_status: int | None = None
    if error is not None:
        error_class = type(error).__name__
        error_message = str(error)[:2000]
        http_status = getattr(error, "status_code", None)

    request_safe = _to_json_safe(request_payload)

    row = {
        "project_id": db.project_id,
        "run_id": current_run_id_var.get() or db.run_id,
        "call_id": current_call_id_var.get(),
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "latency_ms": latency_ms,
        "model": model,
        "request_json": request_safe,
        "response_json": response_json,
        "usage": usage,
        "stop_reason": stop_reason,
        "error_class": error_class,
        "error_message": error_message,
        "http_status": http_status,
        "source": source,
        "streamed": streamed,
    }
    try:
        await db._execute(db.client.table("llm_boundary_exchanges").insert(row))
    except Exception as exc:
        log.error(
            "llm-boundary: insert failed (source=%s model=%s): %s",
            source,
            model,
            exc,
            exc_info=True,
        )
        try:
            payload_size = len(json.dumps(request_safe, default=str))
            log.error("llm-boundary: dropped request_json size=%d bytes", payload_size)
        except Exception:
            pass
