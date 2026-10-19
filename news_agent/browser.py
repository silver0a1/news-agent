"""Page browsing with Playwright: load a URL in headless Chromium and extract its main text."""

import os
from dataclasses import dataclass
from pathlib import Path

BLOCKED_RESOURCES = {"image", "media", "font", "stylesheet"}
SKIPPED_EXTENSIONS = (".pdf", ".zip", ".gz", ".tar", ".mp4", ".mp3", ".png", ".jpg", ".jpeg", ".gif")
VENDOR_DIR = Path(__file__).parent / "vendor" / "readability"

# Shared by both extractors: readable text from headings, paragraphs, list items and quotes.
BLOCK_TEXT_JS = r"""
  const blockText = el => {
    const blocks = [...el.querySelectorAll('h1,h2,h3,h4,p,li,pre,blockquote')]
      .map(b => (b.textContent || '').replace(/\s+/g, ' ').trim())
      .filter(Boolean);
    return blocks.length ? blocks.join('\n\n') : (el.textContent || '').replace(/\s+/g, ' ').trim();
  };
"""

# Fallback extractor. Picks <article>, else <main>, else the element holding the most <p> text,
# after removing navigation and other page chrome.
EXTRACT_JS = r"""
() => {""" + BLOCK_TEXT_JS + r"""
  const drop = 'script,style,noscript,nav,header,footer,aside,form,iframe,svg,' +
               '[role=navigation],[role=banner],[role=contentinfo],[aria-hidden=true]';
  const root = document.body ? document.body.cloneNode(true) : null;
  if (!root) return {title: document.title || '', text: ''};
  root.querySelectorAll(drop).forEach(el => el.remove());

  const textLen = el => (el.innerText || el.textContent || '').trim().length;
  const pick = () => {
    const articles = [...root.querySelectorAll('article')];
    if (articles.length) return articles.reduce((a, b) => textLen(a) >= textLen(b) ? a : b);
    const main = root.querySelector('main, [role=main]');
    if (main) return main;
    const scores = new Map();
    root.querySelectorAll('p').forEach(p => {
      const parent = p.parentElement;
      if (parent) scores.set(parent, (scores.get(parent) || 0) + textLen(p));
    });
    let best = root, bestScore = 0;
    scores.forEach((score, el) => { if (score > bestScore) { best = el; bestScore = score; } });
    return best;
  };

  return {title: document.title || '', text: blockText(pick())};
}
"""


def _reader_js():
    """Reader-mode extractor: Mozilla Readability (vendored), gated by isProbablyReaderable.

    Both library files are inlined into the function body so nothing leaks into the page's
    globals, and page.evaluate runs over CDP, so a site's CSP can't block it.
    """
    readerable_src = (VENDOR_DIR / "Readability-readerable.js").read_text(encoding="utf-8")
    readability_src = (VENDOR_DIR / "Readability.js").read_text(encoding="utf-8")
    return ("() => {\n" + readerable_src + "\n" + readability_src + "\n" + BLOCK_TEXT_JS + r"""
  const out = {readerable: false, title: '', text: '', error: ''};
  try {
    out.readerable = isProbablyReaderable(document);
  } catch (e) {
    out.error = 'isProbablyReaderable: ' + e;
    return out;
  }
  if (!out.readerable) return out;
  try {
    // Readability mutates the document it is given, so hand it a clone.
    const article = new Readability(document.cloneNode(true)).parse();
    if (article && article.content) {
      const holder = document.createElement('div');
      holder.innerHTML = article.content;
      out.title = article.title || '';
      out.text = blockText(holder);
    }
  } catch (e) {
    out.error = 'Readability: ' + e;
  }
  return out;
}
""")


READER_JS = _reader_js()


@dataclass
class PageResult:
    url: str
    status: str            # "ok" | "failed" | "skipped"
    final_url: str = ""
    title: str = ""
    text: str = ""
    error: str = ""
    method: str = ""                  # "readability" | "fallback" ("" when nothing was extracted)
    readerable: bool | None = None    # isProbablyReaderable verdict; None if it never ran


class PageFetcher:
    """One browser per `with` block; one isolated context per page.

        with PageFetcher(timeout_s=20) as fetcher:
            result = fetcher.fetch("https://example.com/post")
    """

    def __init__(self, timeout_s=20, headless=True, chromium_path="", readability_min_chars=500):
        self.timeout_ms = int(timeout_s * 1000)
        self.readability_min_chars = readability_min_chars
        self.headless = headless
        self.chromium_path = chromium_path or os.getenv("NEWS_AGENT_CHROMIUM_PATH", "")
        self._pw = None
        self._browser = None

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        launch = {"headless": self.headless}
        if self.chromium_path:
            launch["executable_path"] = self.chromium_path
        # Chromium ignores proxy env vars; pass them through like requests does.
        proxy = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
        if proxy:
            launch["proxy"] = {"server": proxy}
            bypass = os.getenv("NO_PROXY") or os.getenv("no_proxy")
            if bypass:
                launch["proxy"]["bypass"] = bypass
        try:
            self._browser = self._pw.chromium.launch(**launch)
        except Exception:
            self._pw.stop()
            raise
        return self

    def __exit__(self, *exc):
        try:
            if self._browser:
                self._browser.close()
        finally:
            if self._pw:
                self._pw.stop()
            self._browser = self._pw = None

    @staticmethod
    def _block_heavy(route):
        if route.request.resource_type in BLOCKED_RESOURCES:
            route.abort()
        else:
            route.continue_()

    def fetch(self, url):
        if not url.startswith(("http://", "https://", "file://")):
            return PageResult(url=url, status="skipped", error="unsupported scheme")
        if url.lower().split("?")[0].endswith(SKIPPED_EXTENSIONS):
            return PageResult(url=url, status="skipped", error="non-HTML resource")

        context = self._browser.new_context(java_script_enabled=True)
        try:
            page = context.new_page()
            page.set_default_timeout(self.timeout_ms)
            page.route("**/*", self._block_heavy)
            response = page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            content_type = (response.headers.get("content-type", "") if response else "")
            if content_type and "html" not in content_type:
                return PageResult(url=url, status="skipped", final_url=page.url,
                                  error=f"content-type {content_type}")
            if response and response.status >= 400:
                return PageResult(url=url, status="failed", final_url=page.url,
                                  error=f"HTTP {response.status}")
            try:
                # Give client-rendered pages a moment to fill in, but don't wait on long-polling.
                page.wait_for_load_state("networkidle", timeout=min(5000, self.timeout_ms))
            except Exception:
                pass
            return self._extract(page, url)
        except Exception as e:
            return PageResult(url=url, status="failed", error=f"{type(e).__name__}: {e}"[:300])
        finally:
            context.close()

    def _extract(self, page, url):
        """Reader mode first; the heuristic extractor when the page isn't readerable or
        Readability returns nothing usable (null, an exception, or under the minimum length)."""
        try:
            reader = page.evaluate(READER_JS)
        except Exception as e:
            reader = {"readerable": None, "text": "", "error": f"reader script: {e}"[:200]}
        readerable = reader.get("readerable")
        reader_text = reader.get("text") or ""
        if readerable and len(reader_text) >= self.readability_min_chars:
            return PageResult(url=url, status="ok", final_url=page.url, method="readability",
                              readerable=True, title=reader.get("title") or page.title(),
                              text=reader_text)
        extracted = page.evaluate(EXTRACT_JS)
        return PageResult(url=url, status="ok", final_url=page.url, method="fallback",
                          readerable=readerable, title=extracted.get("title", ""),
                          text=extracted.get("text", ""), error=reader.get("error", ""))
