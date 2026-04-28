"""Unit tests for the DB retry helpers in rumil.database."""

from unittest.mock import MagicMock

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


def _retry_state(exc: BaseException, attempt_number: int) -> MagicMock:
    """Build a minimal RetryCallState stand-in for stop-callback tests."""
    state = MagicMock()
    state.attempt_number = attempt_number
    state.outcome.exception.return_value = exc
    return state


def test_stop_uses_smaller_cap_for_statement_timeout():
    with override_settings(max_db_retries="10", max_db_statement_timeout_retries="3"):
        timeout_exc = _api_error("57014")
        # Below cap: don't stop
        assert not _stop_after_db_retries(_retry_state(timeout_exc, attempt_number=2))
        # At cap: stop
        assert _stop_after_db_retries(_retry_state(timeout_exc, attempt_number=3))


def test_stop_uses_default_cap_for_other_db_errors():
    with override_settings(max_db_retries="10", max_db_statement_timeout_retries="3"):
        other_exc = httpx.ReadTimeout("slow")
        # Below the higher cap: don't stop, even though we'd be over the timeout cap
        assert not _stop_after_db_retries(_retry_state(other_exc, attempt_number=5))
        # At the higher cap: stop
        assert _stop_after_db_retries(_retry_state(other_exc, attempt_number=10))
