"""Subscribe to a trace broadcast channel and forward selected events.

Consumer-side counterpart to ``broadcast.Broadcaster``. Orchestrators send
trace events to channel ``trace:{run_id}`` via the Supabase Realtime HTTP
API; this module subscribes to the same channel over WebSocket and hands
each event to a caller-supplied callback. Used by the chat endpoint to
stream live orchestrator progress into its SSE stream.

The subscriber is intentionally best-effort: any connection or decode
failure is logged and swallowed so that a subscription problem can never
break the orchestrator run it's observing.
"""

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from realtime._async.client import AsyncRealtimeClient
from realtime.types import BroadcastPayload

log = logging.getLogger(__name__)


# Events we surface as chat progress messages.
_SURFACED_EVENTS: frozenset[str] = frozenset(
    {
        "dispatch_executed",
        "context_built",
        "moves_executed",
        "review_complete",
        "error",
        "warning",
        "scoring_completed",
        "dispatches_planned",
        "view_created",
        "phase_skipped",
        "global_phase_completed",
    }
)

# Events we explicitly drop — too noisy (LLM exchanges, tool calls, page
# loads) or too internal (agent/subagent lifecycle, post-run pipelines)
# to surface in a chat progress stream. Every TraceEvent variant in
# ``rumil.tracing.trace_events.TraceEvent`` must appear in either
# ``_SURFACED_EVENTS`` or ``_SUPPRESSED_EVENTS`` — see the contract
# test in tests/test_registry_contracts.py. Adding a new event? Add it
# to one of these sets when you define it.
_SUPPRESSED_EVENTS: frozenset[str] = frozenset(
    {
        "llm_exchange",
        "tool_call",
        "load_page",
        "subagent_started",
        "subagent_completed",
        "agent_started",
        "explore_page",
        "render_question_subgraph",
        "evaluation_complete",
        "reassess_triggered",
        "affected_pages_identified",
        "update_subgraph_computed",
        "update_plan_created",
        "claim_reassessed",
        "grounding_tasks_generated",
        "web_research_complete",
        "link_subquestions_complete",
        "update_view_phase_completed",
    }
)


def _truncate(s: str, n: int = 40) -> str:
    s = s.strip()
    return s if len(s) <= n else s[: n - 1] + "\u2026"


def format_trace_event(payload: dict[str, Any]) -> str | None:
    """Format a broadcast event payload as a short chat progress line.

    Returns None for events we don't want to surface. Never raises — a
    malformed payload returns None so the subscriber can move on.
    """
    try:
        event = payload.get("event")
        if not event or event not in _SURFACED_EVENTS:
            return None

        if event == "dispatch_executed":
            child = payload.get("child_call_type", "?")
            headline = _truncate(payload.get("question_headline") or "", 40)
            if headline:
                return f"dispatch: {child} on '{headline}'"
            return f"dispatch: {child}"

        if event == "context_built":
            working = payload.get("working_context_page_ids") or []
            preloaded = payload.get("preloaded_page_ids") or []
            total = len(working) + len(preloaded)
            budget = payload.get("budget")
            if budget is not None:
                return f"context built ({total} pages, budget={budget})"
            return f"context built ({total} pages)"

        if event == "moves_executed":
            moves = payload.get("moves") or []
            count = len(moves)
            if count == 0:
                return "moves: none"
            types = [m.get("type", "?") for m in moves[:3]]
            suffix = "" if count <= 3 else f" +{count - 3} more"
            return f"moves: {count} ({', '.join(types)}{suffix})"

        if event == "review_complete":
            remaining = payload.get("remaining_fruit")
            confidence = payload.get("confidence")
            parts = []
            if remaining is not None:
                parts.append(f"remaining_fruit={remaining}")
            if confidence is not None:
                parts.append(f"confidence={confidence}")
            return f"review complete ({', '.join(parts)})" if parts else "review complete"

        if event == "error":
            msg = _truncate(payload.get("message") or "", 120)
            phase = payload.get("phase") or ""
            return f"error [{phase}]: {msg}" if phase else f"error: {msg}"

        if event == "warning":
            msg = _truncate(payload.get("message") or "", 120)
            return f"warning: {msg}"

        if event == "scoring_completed":
            sub = len(payload.get("subquestion_scores") or [])
            claims = len(payload.get("claim_scores") or [])
            return f"scoring complete ({sub} subq, {claims} claims)"

        if event == "dispatches_planned":
            dispatches = payload.get("dispatches") or []
            types = [d.get("call_type", "?") for d in dispatches[:4]]
            suffix = "" if len(dispatches) <= 4 else f" +{len(dispatches) - 4} more"
            return f"dispatches planned: {len(dispatches)} ({', '.join(types)}{suffix})"

        if event == "view_created":
            headline = _truncate(payload.get("view_headline") or "", 40)
            return f"view created: '{headline}'" if headline else "view created"

        if event == "phase_skipped":
            phase = payload.get("phase") or "?"
            reason = _truncate(payload.get("reason") or "", 80)
            return f"phase skipped: {phase} ({reason})" if reason else f"phase skipped: {phase}"

        if event == "global_phase_completed":
            phase = payload.get("phase") or "?"
            outcome = _truncate(payload.get("outcome") or "", 80)
            return f"phase done: {phase} ({outcome})" if outcome else f"phase done: {phase}"

        return None
    except Exception as e:
        log.debug("format_trace_event failed: %s", e)
        return None


def _realtime_url(supabase_url: str) -> str:
    """Convert a Supabase HTTP(S) URL into the realtime WebSocket URL."""
    if supabase_url.startswith("https://"):
        return "wss://" + supabase_url[len("https://") :].rstrip("/") + "/realtime/v1"
    if supabase_url.startswith("http://"):
        return "ws://" + supabase_url[len("http://") :].rstrip("/") + "/realtime/v1"
    # Already a ws(s) URL or something unusual — return as-is with path appended.
    return supabase_url.rstrip("/") + "/realtime/v1"


async def stream_run_events(
    run_id: str,
    supabase_url: str,
    supabase_key: str,
    on_event: Callable[[dict[str, Any]], Any],
    subscribed: asyncio.Event | None = None,
) -> None:
    """Subscribe to ``trace:{run_id}`` and call ``on_event(payload)`` per message.

    Runs until cancelled. Any connection or decode error is logged and
    swallowed — this task must never raise into the orchestrator run.

    ``on_event`` receives the broadcast ``payload`` dict (the same shape
    ``Broadcaster.send`` emits: a dumped TraceEvent with ``ts`` and
    ``call_id`` fields added). Exceptions from the callback are logged
    and ignored.

    ``subscribed``: caller-supplied event that is set once
    ``channel.subscribe()`` has returned — callers can await it before
    starting the work they want to observe, to avoid missing early
    events. The event is *also* set on subscription failure (so callers
    awaiting it don't hang forever) — callers shouldn't treat "set"
    as "succeeded"; the stream itself is best-effort.
    """
    ws_url = _realtime_url(supabase_url)
    client = AsyncRealtimeClient(ws_url, token=supabase_key, auto_reconnect=True)
    channel = None
    try:
        await client.connect()
        channel = client.channel(f"trace:{run_id}")

        def _handle(payload: BroadcastPayload) -> None:
            # Supabase Realtime wraps the broadcast into {event, payload, ...}.
            # We want the inner payload, which is the dumped TraceEvent dict.
            try:
                inner = payload.get("payload")
                if not isinstance(inner, dict):
                    return
                on_event(inner)
            except Exception as e:
                log.debug("trace event callback error: %s", e)

        channel.on_broadcast("*", _handle)
        await channel.subscribe()
        if subscribed is not None:
            subscribed.set()

        # Keep the task alive; the realtime client runs its own receive
        # loop in background tasks, so we just wait until cancelled.
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.warning("trace subscription for run %s failed: %s", run_id[:8], e)
        if subscribed is not None:
            # Release any caller awaiting readiness so they can proceed
            # without streaming rather than hang.
            subscribed.set()
    finally:
        try:
            if channel is not None:
                await channel.unsubscribe()
        except Exception as e:
            log.debug("channel unsubscribe error (non-fatal): %s", e)
        try:
            await client.close()
        except Exception as e:
            log.debug("realtime client close error (non-fatal): %s", e)
