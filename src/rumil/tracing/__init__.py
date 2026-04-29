"""rumil tracing — in-house CallTrace plus Langfuse Cloud integration.

For LLM observability, opt any function into Langfuse tracing with one line:

    from rumil.tracing import observe

    @observe()
    async def my_function(...): ...

When Langfuse is configured (LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY in
settings), this emits a span to the configured project. When unset, it's a
no-op and your function runs unmodified.
"""

from langfuse import observe, propagate_attributes

from rumil.tracing.langfuse_client import (
    flush_langfuse,
    get_langfuse,
    langfuse_trace_url_for_current_observation,
    langfuse_trace_url_for_trace_id,
    phase_span,
)

__all__ = [
    "flush_langfuse",
    "get_langfuse",
    "langfuse_trace_url_for_current_observation",
    "langfuse_trace_url_for_trace_id",
    "observe",
    "phase_span",
    "propagate_attributes",
]
