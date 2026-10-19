from pathlib import Path

import pytest

from news_agent.browser import PageFetcher

FIXTURE = Path(__file__).parent / "fixtures" / "article.html"


@pytest.fixture(scope="module")
def fetcher():
    try:
        with PageFetcher(timeout_s=15) as f:
            yield f
    except Exception as e:  # no Chromium available in this environment
        pytest.skip(f"Playwright Chromium unavailable: {e}")


def test_extracts_article_without_page_chrome(fetcher):
    result = fetcher.fetch(FIXTURE.resolve().as_uri())
    assert result.status == "ok"
    assert result.title == "Fixture Article"
    assert "doubles token throughput" in result.text
    for chrome in ("navigation", "newsletter", "Copyright"):
        assert chrome not in result.text


def test_skips_non_html_and_bad_schemes(fetcher):
    assert fetcher.fetch("https://example.com/paper.pdf").status == "skipped"
    assert fetcher.fetch("ftp://example.com").status == "skipped"


def test_missing_page_reports_failure(fetcher):
    result = fetcher.fetch((FIXTURE.parent / "missing.html").resolve().as_uri())
    assert result.status == "failed" and result.error


READER_FIXTURE = Path(__file__).parent / "fixtures" / "reader_article.html"


def test_readerable_page_uses_readability(fetcher):
    result = fetcher.fetch(READER_FIXTURE.resolve().as_uri())
    assert result.status == "ok"
    assert result.method == "readability" and result.readerable is True
    assert result.title == "Open weights model tops coding benchmark"
    assert "single consumer graphics card" in result.text
    # Chrome identified only by class names, which the heuristic extractor can't recognise.
    for chrome in ("Subscribe today", "horoscope", "245 more comments", "Cookie settings"):
        assert chrome not in result.text


def test_unreaderable_page_falls_back(fetcher):
    result = fetcher.fetch(FIXTURE.resolve().as_uri())
    assert result.method == "fallback" and result.readerable is False
    assert "doubles token throughput" in result.text


def test_short_readability_output_falls_back(fetcher):
    previous = fetcher.readability_min_chars
    fetcher.readability_min_chars = 100_000
    try:
        result = fetcher.fetch(READER_FIXTURE.resolve().as_uri())
    finally:
        fetcher.readability_min_chars = previous
    assert result.method == "fallback" and result.readerable is True
    assert "single consumer graphics card" in result.text
