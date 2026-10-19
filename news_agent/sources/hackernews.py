"""Hacker News source, via the public Firebase API."""

import html
import re
from urllib.parse import urlparse

import requests

API = "https://hacker-news.firebaseio.com/v0"


def keyword_pattern(keywords):
    """Whole-word matcher, so 'ai' matches "AI agents" but not "said" or "rain"."""
    alternatives = sorted((re.escape(k) for k in keywords), key=len, reverse=True)
    return re.compile(r"\b(?:" + "|".join(alternatives) + r")\b", re.IGNORECASE)


def html_to_text(fragment):
    text = re.sub(r"<p>", "\n\n", fragment or "")
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


class HackerNewsSource:
    def __init__(self, keywords, session=None, timeout=10):
        self.pattern = keyword_pattern(keywords)
        self.session = session or requests.Session()
        self.timeout = timeout

    def _get(self, path):
        response = self.session.get(f"{API}/{path}.json", timeout=self.timeout)
        response.raise_for_status()
        return response.json()

    def top_story_ids(self, limit):
        return (self._get("topstories") or [])[:limit]

    def item(self, item_id):
        return self._get(f"item/{item_id}")

    def matches(self, title):
        return bool(self.pattern.search(title or ""))

    def candidates(self, limit):
        """Yield relevant top stories as dicts, with their rank on the front page."""
        for rank, story_id in enumerate(self.top_story_ids(limit), start=1):
            try:
                story = self.item(story_id)
            except requests.RequestException as e:
                print(f"⚠️  Error fetching story {story_id}: {e}")
                continue
            if not story or story.get("type") != "story" or not self.matches(story.get("title")):
                continue
            url = story.get("url", "")
            yield {
                "hn_id": story["id"],
                "title": story["title"],
                "url": url,
                "domain": urlparse(url).netloc.removeprefix("www.") if url else "news.ycombinator.com",
                "score": story.get("score", 0),
                "comments": story.get("descendants", 0),
                "posted_at": story.get("time", 0),
                "rank": rank,
                "self_text": html_to_text(story.get("text", "")),
                "kids": story.get("kids", []),
            }

    def comments(self, kids, limit):
        """Top-level comments in HN's ranking order, skipping deleted/dead ones."""
        out = []
        for comment_id in kids:
            if len(out) >= limit:
                break
            try:
                comment = self.item(comment_id)
            except requests.RequestException:
                continue
            if not comment or comment.get("deleted") or comment.get("dead") or not comment.get("text"):
                continue
            out.append({
                "comment_id": comment["id"],
                "author": comment.get("by", ""),
                "text": html_to_text(comment["text"]),
            })
        return out
