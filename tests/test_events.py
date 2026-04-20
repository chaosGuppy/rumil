"""Tests for the in-process event bus."""

from __future__ import annotations

import logging

import pytest

from rumil.events import (
    EventBus,
    PageCreatedEvent,
    fire,
    handler_count,
    isolated_bus,
    register,
    unregister,
)
from rumil.models import PageType


def _make_event(page_id: str = "p1") -> PageCreatedEvent:
    return PageCreatedEvent(
        page_id=page_id,
        page_type=PageType.QUESTION,
        run_id="r1",
        staged=False,
    )


async def test_fire_delivers_to_registered_handler():
    bus = EventBus()
    received: list[PageCreatedEvent] = []

    async def handler(event: PageCreatedEvent) -> None:
        received.append(event)

    bus.register(PageCreatedEvent, handler)
    await bus.fire(_make_event("abc"))

    assert len(received) == 1
    assert received[0].page_id == "abc"


async def test_fire_with_no_handlers_is_noop():
    bus = EventBus()
    await bus.fire(_make_event())


async def test_multiple_handlers_fire_in_registration_order():
    bus = EventBus()
    order: list[str] = []

    async def first(_: PageCreatedEvent) -> None:
        order.append("first")

    async def second(_: PageCreatedEvent) -> None:
        order.append("second")

    bus.register(PageCreatedEvent, first)
    bus.register(PageCreatedEvent, second)
    await bus.fire(_make_event())

    assert order == ["first", "second"]


async def test_raising_handler_does_not_block_others(caplog):
    bus = EventBus()
    reached: list[str] = []

    async def bad(_: PageCreatedEvent) -> None:
        raise RuntimeError("boom")

    async def good(_: PageCreatedEvent) -> None:
        reached.append("good")

    bus.register(PageCreatedEvent, bad)
    bus.register(PageCreatedEvent, good)

    with caplog.at_level(logging.ERROR, logger="rumil.events"):
        await bus.fire(_make_event())

    assert reached == ["good"]
    assert any("boom" in record.message or "boom" in str(record.exc_info) for record in caplog.records)


async def test_unregister_removes_handler():
    bus = EventBus()
    received: list[PageCreatedEvent] = []

    async def handler(event: PageCreatedEvent) -> None:
        received.append(event)

    bus.register(PageCreatedEvent, handler)
    bus.unregister(PageCreatedEvent, handler)
    await bus.fire(_make_event())

    assert received == []
    assert bus.handler_count(PageCreatedEvent) == 0


async def test_unregister_unknown_handler_is_silent():
    bus = EventBus()

    async def never_registered(_: PageCreatedEvent) -> None:
        pass

    bus.unregister(PageCreatedEvent, never_registered)


def test_handler_count_reflects_registrations():
    bus = EventBus()

    async def h(_: PageCreatedEvent) -> None:
        pass

    assert bus.handler_count(PageCreatedEvent) == 0
    bus.register(PageCreatedEvent, h)
    assert bus.handler_count(PageCreatedEvent) == 1
    bus.register(PageCreatedEvent, h)
    assert bus.handler_count(PageCreatedEvent) == 2


async def test_isolated_bus_restores_default_on_exit():
    received: list[PageCreatedEvent] = []

    async def outer_handler(event: PageCreatedEvent) -> None:
        received.append(event)

    register(PageCreatedEvent, outer_handler)
    try:
        with isolated_bus() as inner:
            assert handler_count(PageCreatedEvent) == 0
            inner_received: list[PageCreatedEvent] = []

            async def inner_handler(event: PageCreatedEvent) -> None:
                inner_received.append(event)

            inner.register(PageCreatedEvent, inner_handler)
            await fire(_make_event("inside"))
            assert len(inner_received) == 1
            assert received == []

        await fire(_make_event("outside"))
        assert len(received) == 1
        assert received[0].page_id == "outside"
    finally:
        unregister(PageCreatedEvent, outer_handler)


async def test_isolated_bus_restores_even_on_exception():
    async def h(_: PageCreatedEvent) -> None:
        pass

    register(PageCreatedEvent, h)
    try:
        with pytest.raises(ValueError):
            with isolated_bus():
                assert handler_count(PageCreatedEvent) == 0
                raise ValueError("boom")

        assert handler_count(PageCreatedEvent) == 1
    finally:
        unregister(PageCreatedEvent, h)


def test_page_created_event_round_trips_through_json():
    event = _make_event("abc")
    data = event.model_dump()
    assert data["event_type"] == "page_created"
    restored = PageCreatedEvent.model_validate(data)
    assert restored == event
