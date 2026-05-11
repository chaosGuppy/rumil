"""Regression tests for versus._langfuse.observe call-time enable check."""

import importlib

import pytest
import versus._langfuse as vlf


@pytest.fixture
def reload_vlf(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    importlib.reload(vlf)
    yield vlf
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    importlib.reload(vlf)


def test_observe_uses_real_decorator_when_key_added_after_decoration(
    reload_vlf, monkeypatch: pytest.MonkeyPatch
):
    real_calls: list[tuple[tuple, dict]] = []

    def fake_real_decorator(**_kwargs):
        def wrap(fn):
            def inner(*args, **kwargs):
                real_calls.append((args, kwargs))
                return fn(*args, **kwargs)

            return inner

        return wrap

    monkeypatch.setattr(reload_vlf, "_observe", fake_real_decorator)
    monkeypatch.setattr(reload_vlf, "_HAS_LANGFUSE", True)

    @reload_vlf.observe(name="test")
    def my_fn(x: int) -> int:
        return x * 2

    assert my_fn(3) == 6
    assert real_calls == []

    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")

    assert my_fn(4) == 8
    assert real_calls == [((4,), {})]


def test_observe_skips_real_decorator_when_key_unset(reload_vlf, monkeypatch: pytest.MonkeyPatch):
    real_calls: list[int] = []

    def fake_real_decorator(**_kwargs):
        def wrap(fn):
            def inner(*args, **kwargs):
                real_calls.append(1)
                return fn(*args, **kwargs)

            return inner

        return wrap

    monkeypatch.setattr(reload_vlf, "_observe", fake_real_decorator)
    monkeypatch.setattr(reload_vlf, "_HAS_LANGFUSE", True)

    @reload_vlf.observe(name="test")
    def my_fn(x: int) -> int:
        return x + 1

    assert my_fn(10) == 11
    assert my_fn(20) == 21
    assert real_calls == []


def test_observe_returns_pure_noop_when_package_missing(
    reload_vlf, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(reload_vlf, "_HAS_LANGFUSE", False)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")

    @reload_vlf.observe(name="test")
    def my_fn(x: int) -> int:
        return x

    assert my_fn(7) == 7
