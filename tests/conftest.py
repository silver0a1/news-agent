import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from news_agent.storage import Storage  # noqa: E402


@pytest.fixture
def storage():
    with Storage(":memory:") as s:
        yield s


def make_story(hn_id, title="New LLM inference engine", url=None, score=100, rank=1, **extra):
    story = {
        "hn_id": hn_id, "title": title,
        "url": f"https://example.com/{hn_id}" if url is None else url,
        "domain": "example.com", "score": score, "comments": 3, "posted_at": 1_700_000_000,
        "rank": rank, "self_text": "", "kids": [hn_id * 10 + 1, hn_id * 10 + 2],
    }
    story.update(extra)
    return story
