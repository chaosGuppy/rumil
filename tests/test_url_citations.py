"""Unit tests for URL citation rewriting."""

import uuid

import pytest

from rumil.moves.create_claim import rewrite_url_citations


def _fake_id() -> str:
    return str(uuid.uuid4())


class TestRewriteUrlCitations:
    def test_basic_replacement(self):
        pid = _fake_id()
        cache = {"https://example.com/article": pid}
        result = rewrite_url_citations(
            "According to [https://example.com/article], rates doubled.", cache
        )
        assert result == f"According to [{pid[:8]}], rates doubled."

    def test_trailing_slash_tolerance(self):
        pid = _fake_id()
        cache = {"https://example.com/article/": pid}
        result = rewrite_url_citations("See [https://example.com/article] for details.", cache)
        assert result == f"See [{pid[:8]}] for details."

    def test_trailing_slash_tolerance_reverse(self):
        pid = _fake_id()
        cache = {"https://example.com/article": pid}
        result = rewrite_url_citations("See [https://example.com/article/] for details.", cache)
        assert result == f"See [{pid[:8]}] for details."

    def test_multiple_citations(self):
        pid1 = _fake_id()
        pid2 = _fake_id()
        cache = {
            "https://a.com/1": pid1,
            "https://b.com/2": pid2,
        }
        result = rewrite_url_citations("[https://a.com/1] agrees with [https://b.com/2].", cache)
        assert result == f"[{pid1[:8]}] agrees with [{pid2[:8]}]."

    def test_no_citations_passthrough(self):
        cache = {"https://example.com": _fake_id()}
        text = "No citations here."
        assert rewrite_url_citations(text, cache) == text

    def test_unmatched_url_raises(self):
        cache = {"https://example.com/known": _fake_id()}
        with pytest.raises(ValueError, match="do not match any scraped source"):
            rewrite_url_citations("See [https://unknown.com/page] for details.", cache)

    def test_unmatched_error_lists_urls(self):
        with pytest.raises(ValueError, match=r"https://bad\.com/x"):
            rewrite_url_citations("See [https://bad.com/x].", {})

    def test_non_url_brackets_ignored(self):
        pid = _fake_id()
        cache = {"https://example.com": pid}
        result = rewrite_url_citations(
            "The value [42] and [https://example.com] are different.", cache
        )
        assert result == f"The value [42] and [{pid[:8]}] are different."
