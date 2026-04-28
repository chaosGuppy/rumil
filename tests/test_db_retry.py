"""Unit tests for the DB retry helpers in rumil.database."""

from types import SimpleNamespace

import httpx
from postgrest.exceptions import APIError

from rumil.database import (
    _is_retryable_api_error,
    _is_statement_timeout,
    _should_retry_db_exception,
    _stop_after_db_retries,
)
from rumil.settings import override_settings


def _api_error(code: str | None) -> APIError:
    return APIError({"message": "x", "code": code, "hint": None, "details": None})


def test_is_statement_timeout_recognizes_57014():
    assert _is_statement_timeout(_api_error("57014"))


def test_is_statement_timeout_rejects_other_codes():
    assert not _is_statement_timeout(_api_error("PGRST116"))
    assert not _is_statement_timeout(_api_error("503"))
    assert not _is_statement_timeout(_api_error(None))
    assert not _is_statement_timeout(httpx.ReadTimeout("x"))


def test_should_retry_db_exception_includes_statement_timeout():
    assert _should_retry_db_exception(_api_error("57014"))


def test_should_retry_db_exception_still_excludes_4xx():
    assert not _should_retry_db_exception(_api_error("PGRST116"))
    assert not _should_retry_db_exception(_api_error("400"))


def test_is_retryable_api_error_unchanged_for_5xx():
    assert _is_retryable_api_error(_api_error("503"))
    assert not _is_retryable_api_error(_api_error("400"))


def _fresh_retry_state() -> SimpleNamespace:
    """A stand-in for tenacity's per-call RetryCallState. Each test that
    drives a sequence of attempts shares one of these across calls so the
    counters _stop_after_db_retries mutates persist as they would in a real
    retry."""
    return SimpleNamespace(outcome=None)


def _feed(state: SimpleNamespace, exc: BaseException) -> bool:
    """Set the latest outcome to *exc* and call _stop_after_db_retries.

    SimpleNamespace stands in for tenacity's RetryCallState — it has the
    duck-typed shape the helper actually reads (``.outcome.exception()``
    and the custom ``_db_attempts_by_class`` we attach), without needing
    the rest of tenacity's internals.
    """
    state.outcome = SimpleNamespace(exception=lambda: exc)
    return _stop_after_db_retries(state)  # type: ignore[arg-type]


def test_stop_uses_smaller_cap_for_statement_timeout():
    with override_settings(max_db_retries="10", max_db_statement_timeout_retries="3"):
        state = _fresh_retry_state()
        timeout_exc = _api_error("57014")
        assert not _feed(state, timeout_exc)  # 1
        assert not _feed(state, timeout_exc)  # 2
        assert _feed(state, timeout_exc)  # 3 → stop


def test_stop_uses_default_cap_for_other_db_errors():
    with override_settings(max_db_retries="10", max_db_statement_timeout_retries="3"):
        state = _fresh_retry_state()
        other = httpx.ReadTimeout("slow")
        # 9 attempts: still going (already past the timeout cap, but that's
        # a separate budget, so it doesn't apply here)
        for _ in range(9):
            assert not _feed(state, other)
        # 10th attempt stops
        assert _feed(state, other)


def test_57014_after_503s_gets_its_own_budget():
    """Reviewer's case: a 57014 that lands after some 503s shouldn't
    inherit the 503s' attempt count. Each class has its own cap."""
    with override_settings(max_db_retries="10", max_db_statement_timeout_retries="3"):
        state = _fresh_retry_state()
        timeout_exc = _api_error("57014")
        other = httpx.ReadTimeout("slow")
        # Burn 5 'other' retries.
        for _ in range(5):
            assert not _feed(state, other)
        # Now 57014 should still get its full 3-attempt budget.
        assert not _feed(state, timeout_exc)  # timeout count = 1
        assert not _feed(state, timeout_exc)  # timeout count = 2
        assert _feed(state, timeout_exc)  # timeout count = 3 → stop


def test_503s_after_57014s_keep_full_other_budget():
    """Symmetric: a 503 stream shouldn't inherit 57014 attempt count."""
    with override_settings(max_db_retries="10", max_db_statement_timeout_retries="3"):
        state = _fresh_retry_state()
        timeout_exc = _api_error("57014")
        other = httpx.ReadTimeout("slow")
        # Burn 2 timeout retries (one shy of the cap).
        assert not _feed(state, timeout_exc)
        assert not _feed(state, timeout_exc)
        # 'other' should still have its full 10-attempt budget.
        for _ in range(9):
            assert not _feed(state, other)
        assert _feed(state, other)
