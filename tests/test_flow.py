import datetime

from conftest import make_story
from news_agent.browser import PageResult
from news_agent.config import Settings
from news_agent.llm import FakeLLMClient
from news_agent.nodes import create_agent_flow


class FakeSource:
    def __init__(self, stories):
        self.stories = stories

    def candidates(self, limit):
        return list(self.stories)[:limit]

    def item(self, hn_id):
        return {"id": hn_id, "kids": [hn_id * 10 + 1]}

    def comments(self, kids, limit):
        return [{"comment_id": k, "author": "u", "text": f"comment {k}"} for k in kids][:limit]


class FakeFetcher:
    fetched = []

    def __init__(self, fail_launch=False):
        self.fail_launch = fail_launch

    def __enter__(self):
        if self.fail_launch:
            raise RuntimeError("no browser")
        return self

    def __exit__(self, *exc):
        pass

    def fetch(self, url):
        FakeFetcher.fetched.append(url)
        if url.endswith("/3"):
            return PageResult(url=url, status="ok", final_url=url, text=f"Article body for {url}",
                              method="fallback", readerable=False)
        return PageResult(url=url, status="ok", final_url=url, text=f"Article body for {url}",
                          method="readability", readerable=True)


def shared_for(storage, stories, fail_launch=False, **settings):
    return {
        "settings": Settings(max_new_per_cycle=3, **settings),
        "storage": storage,
        "llm": FakeLLMClient(),
        "source": FakeSource(stories),
        "fetcher_factory": lambda: FakeFetcher(fail_launch),
    }


def test_full_cycle_stores_everything_and_is_idempotent(storage, capsys):
    FakeFetcher.fetched = []
    # Highest-scoring new stories are fetched first when over the per-cycle cap.
    stories = [make_story(1, score=300), make_story(2, url="", score=200), make_story(3, score=100),
               make_story(4, score=50), make_story(5, score=10)]
    stories[1]["self_text"] = "Ask HN body"
    shared = shared_for(storage, stories)
    create_agent_flow(once=True).run(shared)

    # Per-cycle cap: 3 fetched + analysed now, the rest stay queued.
    assert storage.conn.execute("SELECT COUNT(*) FROM stories").fetchone()[0] == 5
    assert storage.conn.execute("SELECT COUNT(*) FROM analyses").fetchone()[0] == 3
    assert sorted(storage.stories_needing_fetch()) == [4, 5]
    assert storage.get_article(2)["text"] == "Ask HN body"
    assert all(u for u in FakeFetcher.fetched)  # self post was not browsed
    methods = dict(storage.conn.execute("SELECT hn_id, extraction_method FROM articles"))
    assert methods == {1: "readability", 2: "self_post", 3: "fallback"}
    assert ("📑 Extraction: readability 1 · fallback 1 (not readerable 1, too short/failed 0) "
            "· self_post 1 · none 0") in capsys.readouterr().out
    report = storage.latest_report()
    assert report and report["story_count"] == 3

    # Next cycle drains the backlog, report not due yet.
    create_agent_flow(once=True).run(shared)
    assert storage.conn.execute("SELECT COUNT(*) FROM analyses").fetchone()[0] == 5
    assert storage.conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 1

    # Nothing new: no LLM calls at all.
    shared["llm"] = FakeLLMClient()
    create_agent_flow(once=True).run(shared)
    assert shared["llm"].calls == []


def test_browser_failure_still_analyses_from_title_and_comments(storage):
    shared = shared_for(storage, [make_story(7)], fail_launch=True)
    create_agent_flow(once=True).run(shared)
    assert storage.get_article(7)["status"] == "failed"
    assert storage.get_article(7)["extraction_method"] == "none"
    assert storage.conn.execute("SELECT COUNT(*) FROM analyses").fetchone()[0] == 1
    assert "article text unavailable" in shared["llm"].calls[0]


def test_legacy_seen_titles_are_skipped(storage, tmp_path):
    (tmp_path / "seen_stories.csv").write_text("title,first_seen\nOld one,2025-01-01\n")
    storage.import_legacy_csv(tmp_path)
    shared = shared_for(storage, [make_story(8, title="Old one"), make_story(9)])
    create_agent_flow(once=True).run(shared)
    assert not storage.is_known(8) and storage.is_known(9)


def test_report_due_after_interval(storage):
    shared = shared_for(storage, [make_story(1)], report_interval_hours=24)
    storage.save_report("a", "b", 0, "old")
    storage.conn.execute("UPDATE reports SET created_at = ?",
                         ((datetime.datetime.now() - datetime.timedelta(hours=25)).isoformat(),))
    create_agent_flow(once=True).run(shared)
    assert storage.conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 2
