import csv

from conftest import make_story


def test_upsert_dedups_by_id_and_records_snapshots(storage):
    assert storage.upsert_story(make_story(1, score=10)) is True
    assert storage.upsert_story(make_story(1, title="Renamed title", score=25)) is False
    story = storage.get_story(1)
    assert story["title"] == "Renamed title"
    assert story["score"] == 25
    n = storage.conn.execute("SELECT COUNT(*) FROM score_snapshots WHERE hn_id = 1").fetchone()[0]
    assert n == 2


def test_fetch_and_analysis_queues(storage):
    storage.upsert_story(make_story(1))
    storage.upsert_story(make_story(2))
    assert set(storage.stories_needing_fetch()) == {1, 2}

    storage.save_article(1, "ok", text="hello world")
    storage.save_comments(1, [{"comment_id": 11, "author": "a", "text": "nice"}])
    digest = storage.finalize_content(1)
    assert storage.stories_needing_fetch() == [2]
    assert storage.stories_needing_analysis() == [1]

    storage.save_analysis(1, {"summary": "s", "theme": "T", "sentiment": "positive"}, hash_=digest)
    assert storage.stories_needing_analysis() == []

    # Re-fetched page with different content -> needs analysis again.
    storage.save_article(1, "ok", text="hello changed world")
    storage.finalize_content(1)
    assert storage.stories_needing_analysis() == [1]


def test_search_and_reanalysis_replaces_fts_row(storage):
    storage.upsert_story(make_story(1, title="Rust compiler gets faster"))
    storage.save_analysis(1, {"summary": "Compile times drop", "theme": "Rust",
                              "sentiment": "positive", "entities": ["rustc"]})
    assert [r["hn_id"] for r in storage.search('"rust"*')] == [1]
    storage.save_analysis(1, {"summary": "Something else", "theme": "Tooling",
                              "sentiment": "neutral", "entities": []})
    assert storage.search('"times"') == []
    assert [r["hn_id"] for r in storage.search('"tooling"')] == [1]


def test_theme_stats_and_period_query(storage):
    for i, theme in enumerate(["AI", "AI", "Chips"], start=1):
        storage.upsert_story(make_story(i, score=i * 10))
        storage.save_analysis(i, {"summary": "x", "theme": theme, "sentiment": "neutral"})
    stats = storage.theme_stats("2000-01-01", "2999-01-01")
    assert stats["themes"][0] == ("AI", 2)
    items = storage.analyses_between("2000-01-01", "2999-01-01")
    assert [i["hn_id"] for i in items] == [3, 2, 1]  # highest score first


def test_legacy_import_and_csv_export(storage, tmp_path):
    (tmp_path / "seen_stories.csv").write_text("title,first_seen\nOld story,2025-07-18T10:16:28\n")
    (tmp_path / "daily_reports.csv").write_text('date,insights_analyzed,summary\n2025-07-18,15,"Old report"\n')
    counts = storage.import_legacy_csv(tmp_path)
    assert counts == {"seen_stories.csv": 1, "daily_reports.csv": 1}
    assert storage.is_legacy_seen("Old story")
    assert storage.latest_report()["content"] == "Old report"

    storage.upsert_story(make_story(1))
    storage.save_analysis(1, {"summary": "s", "theme": "T", "sentiment": "positive"})
    paths = storage.export_csv(tmp_path / "out")
    with open(tmp_path / "out" / "insights.csv") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["theme"] == "T" and len(paths) == 3


def test_meta_roundtrip(storage):
    assert storage.get_meta("legacy_csv_imported") is None
    storage.set_meta("legacy_csv_imported", "2026-01-01")
    assert storage.get_meta("legacy_csv_imported") == "2026-01-01"
