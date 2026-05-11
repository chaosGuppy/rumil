"""Tests for versus.complete._with_transient_retry.

Behavior pinned: retries on httpx 5xx + transient connection/timeout
errors, passes through 4xx and non-httpx errors immediately, exhausts
after ``max_retries + 1`` attempts and re-raises the last error.

Tests use ``base_delay=0.0`` to avoid sleeping. The retry helper is
pure (no I/O of its own — caller's ``fn`` does any I/O), so we drive
it with stub callables that increment a counter and selectively raise.
"""

import httpx
import pytest

from versus import complete


def _make_response(status: int) -> httpx.Response:
    return httpx.Response(status_code=status, request=httpx.Request("POST", "https://example/"))


def _http_status_error(status: int) -> httpx.HTTPStatusError:
    resp = _make_response(status)
    return httpx.HTTPStatusError(f"HTTP {status}", request=resp.request, response=resp)


def test_returns_value_on_first_attempt() -> None:
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        return "ok"

    result = complete._with_transient_retry(fn, label="t", base_delay=0.0)

    assert result == "ok"
    assert calls["n"] == 1


def test_succeeds_after_transient_5xx() -> None:
    seq = iter([_http_status_error(503), _http_status_error(500), "ok"])

    def fn():
        item = next(seq)
        if isinstance(item, Exception):
            raise item
        return item

    result = complete._with_transient_retry(fn, label="t", base_delay=0.0, max_retries=3)
    assert result == "ok"


def test_exhausts_then_reraises_last_error() -> None:
    err = _http_status_error(500)

    def fn():
        raise err

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        complete._with_transient_retry(fn, label="t", base_delay=0.0, max_retries=2)

    assert exc_info.value is err


def test_attempt_count_is_max_retries_plus_one(mocker) -> None:
    fn = mocker.Mock(side_effect=_http_status_error(503))

    with pytest.raises(httpx.HTTPStatusError):
        complete._with_transient_retry(fn, label="t", base_delay=0.0, max_retries=3)

    assert fn.call_count == 4


@pytest.mark.parametrize("status", (400, 401, 403, 404, 422, 429))
def test_4xx_is_not_retried(status: int, mocker) -> None:
    fn = mocker.Mock(side_effect=_http_status_error(status))

    with pytest.raises(httpx.HTTPStatusError):
        complete._with_transient_retry(fn, label="t", base_delay=0.0, max_retries=3)

    assert fn.call_count == 1


def test_non_httpx_error_is_not_retried(mocker) -> None:
    fn = mocker.Mock(side_effect=ValueError("nope"))

    with pytest.raises(ValueError):
        complete._with_transient_retry(fn, label="t", base_delay=0.0, max_retries=3)

    assert fn.call_count == 1


@pytest.mark.parametrize(
    "exc_factory",
    (
        lambda: httpx.TimeoutException("timed out"),
        lambda: httpx.ReadError("read failed"),
        lambda: httpx.ConnectError("connect failed"),
        lambda: httpx.RemoteProtocolError("protocol error"),
    ),
)
def test_transient_httpx_errors_are_retried(exc_factory) -> None:
    seq = [exc_factory(), exc_factory(), "ok"]
    idx = {"i": 0}

    def fn():
        i = idx["i"]
        idx["i"] += 1
        item = seq[i]
        if isinstance(item, Exception):
            raise item
        return item

    result = complete._with_transient_retry(fn, label="t", base_delay=0.0, max_retries=3)
    assert result == "ok"


def test_label_appears_in_retry_log(capsys) -> None:
    seq = iter([_http_status_error(503), "ok"])

    def fn():
        item = next(seq)
        if isinstance(item, Exception):
            raise item
        return item

    complete._with_transient_retry(fn, label="my-essay | claude-haiku", base_delay=0.0)

    out = capsys.readouterr().out
    assert "[retry]" in out
    assert "my-essay | claude-haiku" in out
    assert "503" in out


def test_zero_max_retries_means_one_attempt(mocker) -> None:
    fn = mocker.Mock(side_effect=_http_status_error(503))

    with pytest.raises(httpx.HTTPStatusError):
        complete._with_transient_retry(fn, label="t", base_delay=0.0, max_retries=0)

    assert fn.call_count == 1
