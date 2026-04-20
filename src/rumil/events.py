"""In-process event bus for workspace lifecycle events.

Producers `fire` typed event payloads; subscribers register async handlers
keyed by the concrete event class they care about. Distinct from tracing: that
module captures per-call diagnostics, this one is an extensibility seam for
optional side effects.

**When to use.** Optional, experiment-style hooks into lifecycle points — e.g.
auto-create a View when a question is created, tee page events to a future
orchestrator. Keeps the trigger site ignorant of who is listening.

**When not to use.** Mandatory workflow. If A *must* do X after Y, call X
directly — an event handler that silently stops running (or is unregistered in
a test) would break the invariant without noticing.

**Semantics to know before firing.** Dispatch is by *exact type*, not
`isinstance` — register on the concrete subclass you want. Handlers run
sequentially in registration order; a raising handler is logged and swallowed
so it can't block others. Fire events **after** the underlying state is
persisted so handlers observe committed state. Tests should scope
registrations with `isolated_bus()` to avoid leaking handlers between cases.

Nothing in this module fires events on its own — integration into concrete
lifecycle points happens in follow-up work. The default bus ships with no
handlers, so importing this module has no runtime effect.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import Literal, TypeVar

from pydantic import BaseModel

from rumil.models import PageType

log = logging.getLogger(__name__)


class Event(BaseModel):
    """Base class for every event payload.

    Subclasses declare a `Literal["..."]` discriminator on `event_type` so
    traces and logs can identify the event even after round-tripping through
    JSON. The base class intentionally has no `event_type` field: pydantic's
    invariance check rejects narrowing `str` to a `Literal` in subclasses.
    """


class PageCreatedEvent(Event):
    event_type: Literal["page_created"] = "page_created"
    page_id: str
    page_type: PageType
    run_id: str | None = None
    staged: bool = False


E = TypeVar("E", bound=Event)
Handler = Callable[[E], Awaitable[None]]


class EventBus:
    """Registry of async handlers keyed by concrete event class.

    Handlers are invoked sequentially in registration order. A handler that
    raises is logged and skipped; other handlers for the same event still run.
    """

    def __init__(self) -> None:
        self._handlers: dict[type[Event], list[Handler]] = {}

    def register(self, event_class: type[E], handler: Handler[E]) -> None:
        self._handlers.setdefault(event_class, []).append(handler)

    def unregister(self, event_class: type[E], handler: Handler[E]) -> None:
        handlers = self._handlers.get(event_class)
        if not handlers:
            return
        try:
            handlers.remove(handler)
        except ValueError:
            return
        if not handlers:
            del self._handlers[event_class]

    async def fire(self, event: Event) -> None:
        handlers = self._handlers.get(type(event), [])
        for handler in handlers:
            try:
                await handler(event)
            except Exception:
                log.exception(
                    "event handler %r raised while handling %s",
                    handler,
                    type(event).__name__,
                )

    def clear(self) -> None:
        self._handlers.clear()

    def handler_count(self, event_class: type[Event]) -> int:
        return len(self._handlers.get(event_class, []))


_default_bus = EventBus()


def register(event_class: type[E], handler: Handler[E]) -> None:
    _default_bus.register(event_class, handler)


def unregister(event_class: type[E], handler: Handler[E]) -> None:
    _default_bus.unregister(event_class, handler)


async def fire(event: Event) -> None:
    await _default_bus.fire(event)


def handler_count(event_class: type[Event]) -> int:
    return _default_bus.handler_count(event_class)


@contextmanager
def isolated_bus() -> Iterator[EventBus]:
    """Swap the module-level default bus for an empty one for the duration.

    Use in tests to avoid cross-test handler leakage. The original bus is
    restored on exit even if the block raises.
    """

    global _default_bus
    previous = _default_bus
    _default_bus = EventBus()
    try:
        yield _default_bus
    finally:
        _default_bus = previous
