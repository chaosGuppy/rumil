"""Tests for rumil_skills._format helpers."""

from rumil_skills._format import (
    print_event,
    print_trace,
    short,
    trace_url,
    truncate,
)

from rumil.settings import override_settings


def test_short_from_string():
    assert short("abcdefghijklmnop") == "abcdefgh"


def test_short_string_shorter_than_eight():
    assert short("abc") == "abc"


def test_short_from_object_with_id_attr():
    class Fake:
        id = "1234567890abcdef"

    assert short(Fake()) == "12345678"


def test_short_falls_back_to_str_when_id_missing():
    class Fake:
        def __str__(self):
            return "XYZ987654321"

    assert short(Fake()) == "XYZ98765"


def test_short_falls_back_when_id_is_not_string():
    class Fake:
        id = 42

    assert short(Fake()).startswith("<")


def test_trace_url_uses_settings_frontend_url():
    with override_settings(frontend_url="http://localhost:4242"):
        assert trace_url("run-abc") == "http://localhost:4242/traces/run-abc"


def test_trace_url_strips_nothing_no_extra_logic():
    with override_settings(frontend_url="http://example.test"):
        assert trace_url("xyz") == "http://example.test/traces/xyz"


def test_print_trace_prints_default_label(capsys):
    with override_settings(frontend_url="http://localhost:9999"):
        print_trace("run-1")
    out = capsys.readouterr().out
    assert out == "trace: http://localhost:9999/traces/run-1\n"


def test_print_trace_custom_label(capsys):
    with override_settings(frontend_url="http://localhost:9999"):
        print_trace("run-2", label="scan")
    out = capsys.readouterr().out
    assert out == "scan: http://localhost:9999/traces/run-2\n"


def test_print_event_formats_symbol_and_message(capsys):
    print_event("✓", "done")
    out = capsys.readouterr().out
    assert out == "✓ done\n"


def test_truncate_returns_text_when_under_limit():
    assert truncate("hello", 80) == "hello"


def test_truncate_at_exact_boundary():
    text = "a" * 80
    assert truncate(text, 80) == text


def test_truncate_shortens_with_ellipsis():
    text = "a" * 100
    result = truncate(text, 10)
    assert len(result) == 10
    assert result.endswith("…")
    assert result == "a" * 9 + "…"


def test_truncate_custom_length():
    result = truncate("abcdefghij", 5)
    assert result == "abcd…"
