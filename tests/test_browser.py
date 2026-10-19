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
