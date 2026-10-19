"""Page browsing with Playwright: load a URL in headless Chromium and extract its main text."""

import os
from dataclasses import dataclass

BLOCKED_RESOURCES = {"image", "media", "font", "stylesheet"}
SKIPPED_EXTENSIONS = (".pdf", ".zip", ".gz", ".tar", ".mp4", ".mp3", ".png", ".jpg", ".jpeg", ".gif")

# Runs in the page. Picks <article>, else <main>, else the element holding the most <p> text,
# after removing navigation and other page chrome.
EXTRACT_JS = r"""
() => {
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

  const el = pick();
  const blocks = [...el.querySelectorAll('h1,h2,h3,h4,p,li,pre,blockquote')]
    .map(b => (b.textContent || '').replace(/\s+/g, ' ').trim())
    .filter(Boolean);
  const text = blocks.length ? blocks.join('\n\n') : (el.textContent || '').replace(/\s+/g, ' ').trim();
  return {title: document.title || '', text};
}
"""


@dataclass
class PageResult:
    url: str
    status: str            # "ok" | "failed" | "skipped"
    final_url: str = ""
    title: str = ""
    text: str = ""
    error: str = ""


class PageFetcher:
    """One browser per `with` block; one isolated context per page.

        with PageFetcher(timeout_s=20) as fetcher:
            result = fetcher.fetch("https://example.com/post")
    """

    def __init__(self, timeout_s=20, headless=True, chromium_path=""):
        self.timeout_ms = int(timeout_s * 1000)
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
            extracted = page.evaluate(EXTRACT_JS)
            return PageResult(url=url, status="ok", final_url=page.url,
                              title=extracted.get("title", ""), text=extracted.get("text", ""))
        except Exception as e:
            return PageResult(url=url, status="failed", error=f"{type(e).__name__}: {e}"[:300])
        finally:
            context.close()
