from news_agent.config import DEFAULT_KEYWORDS
from news_agent.sources.hackernews import HackerNewsSource, html_to_text, keyword_pattern


def test_keywords_match_whole_words_only():
    pattern = keyword_pattern(DEFAULT_KEYWORDS)
    assert pattern.search("New AI model beats benchmarks")
    assert pattern.search("Machine Learning in production")
    assert not pattern.search("He said it would rain")
    assert not pattern.search("Gardening tips for spring")


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


class FakeSession:
    def __init__(self, items):
        self.items = items

    def get(self, url, timeout=None):
        key = url.rsplit("/v0/", 1)[1].removesuffix(".json")
        return FakeResponse(self.items.get(key))


def test_candidates_and_comments():
    items = {
        "topstories": [1, 2, 3],
        "item/1": {"id": 1, "type": "story", "title": "LLM agents in production", "url": "https://www.blog.dev/x",
                   "score": 50, "descendants": 2, "time": 1, "kids": [11, 12, 13]},
        "item/2": {"id": 2, "type": "story", "title": "A recipe for bread", "url": "https://food.com"},
        "item/3": {"id": 3, "type": "story", "title": "Ask HN: Which AI tools do you use?", "text": "<p>Curious &amp; asking"},
        "item/11": {"id": 11, "by": "a", "text": "Great <i>post</i>"},
        "item/12": {"id": 12, "deleted": True},
        "item/13": {"id": 13, "by": "b", "text": "Agree"},
    }
    source = HackerNewsSource(DEFAULT_KEYWORDS, session=FakeSession(items))
    stories = list(source.candidates(10))
    assert [s["hn_id"] for s in stories] == [1, 3]
    assert stories[0]["domain"] == "blog.dev" and stories[0]["rank"] == 1
    assert stories[1]["url"] == "" and stories[1]["self_text"] == "Curious & asking"
    comments = source.comments(stories[0]["kids"], limit=5)
    assert [c["text"] for c in comments] == ["Great post", "Agree"]


def test_html_to_text():
    assert html_to_text("a<p>b &gt; c") == "a\n\nb > c"
