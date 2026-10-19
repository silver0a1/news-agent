"""SQLite storage: one row per story, plus fetched content, analyses, score history and reports."""

import csv
import datetime
import hashlib
import json
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS stories (
    hn_id      INTEGER PRIMARY KEY,
    title      TEXT NOT NULL,
    url        TEXT,
    domain     TEXT,
    posted_at  TEXT,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    self_text  TEXT
);
CREATE INDEX IF NOT EXISTS idx_stories_first_seen ON stories(first_seen);

CREATE TABLE IF NOT EXISTS score_snapshots (
    hn_id    INTEGER NOT NULL REFERENCES stories(hn_id),
    seen_at  TEXT NOT NULL,
    score    INTEGER,
    rank     INTEGER,
    comments INTEGER
);
CREATE INDEX IF NOT EXISTS idx_snapshots_story ON score_snapshots(hn_id, seen_at);

CREATE TABLE IF NOT EXISTS articles (
    hn_id        INTEGER PRIMARY KEY REFERENCES stories(hn_id),
    fetched_at   TEXT NOT NULL,
    status       TEXT NOT NULL,      -- ok | failed | skipped
    final_url    TEXT,
    error        TEXT,
    text         TEXT,
    content_hash TEXT,
    word_count   INTEGER,
    extraction_method TEXT,          -- readability | fallback | self_post | none
    readerable   INTEGER             -- isProbablyReaderable verdict: 1, 0, or NULL if it never ran
);

CREATE TABLE IF NOT EXISTS comments (
    comment_id INTEGER PRIMARY KEY,
    hn_id      INTEGER NOT NULL REFERENCES stories(hn_id),
    position   INTEGER,
    author     TEXT,
    text       TEXT
);
CREATE INDEX IF NOT EXISTS idx_comments_story ON comments(hn_id, position);

CREATE TABLE IF NOT EXISTS analyses (
    hn_id             INTEGER PRIMARY KEY REFERENCES stories(hn_id),
    created_at        TEXT NOT NULL,
    model             TEXT,
    content_hash      TEXT,          -- hash of the content analysed; unchanged hash = skip re-analysis
    summary           TEXT,
    theme             TEXT,
    sentiment         TEXT,
    key_points        TEXT,          -- JSON list
    entities          TEXT,          -- JSON list
    prompt_tokens     INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_analyses_created ON analyses(created_at);

CREATE TABLE IF NOT EXISTS reports (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at        TEXT NOT NULL,
    period_start      TEXT,
    period_end        TEXT,
    story_count       INTEGER,
    content           TEXT,
    prompt_tokens     INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0
);

-- Full-text index over the compact per-story view, used for retrieval instead of raw text.
CREATE VIRTUAL TABLE IF NOT EXISTS analyses_fts USING fts5(
    title, summary, theme, entities, tokenize='porter unicode61'
);

-- Titles seen by the original CSV-based agent (no HN ids), so upgrades don't re-analyse them.
CREATE TABLE IF NOT EXISTS legacy_seen (title TEXT PRIMARY KEY, first_seen TEXT);
"""


def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def content_hash(*parts):
    return hashlib.sha256("\x1f".join(p or "" for p in parts).encode("utf-8")).hexdigest()[:16]


class Storage:
    def __init__(self, path="news_agent.db"):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        self.conn.commit()

    # Columns added after the first release, applied to databases created by older versions.
    MIGRATIONS = {
        "articles": [("extraction_method", "TEXT"), ("readerable", "INTEGER")],
    }

    def _migrate(self):
        for table, columns in self.MIGRATIONS.items():
            existing = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in columns:
                if name not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        self.conn.commit()

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def get_meta(self, key):
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key, value):
        self.conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, str(value)))
        self.conn.commit()

    # --- stories -------------------------------------------------------------------------

    def is_known(self, hn_id):
        return self.conn.execute("SELECT 1 FROM stories WHERE hn_id = ?", (hn_id,)).fetchone() is not None

    def upsert_story(self, story, seen_at=None):
        """Insert or refresh a story and record a score snapshot. Returns True if it was new."""
        seen_at = seen_at or now_iso()
        is_new = not self.is_known(story["hn_id"])
        posted = story.get("posted_at")
        if isinstance(posted, (int, float)) and posted:
            posted = datetime.datetime.fromtimestamp(posted).isoformat(timespec="seconds")
        self.conn.execute(
            """INSERT INTO stories (hn_id, title, url, domain, posted_at, first_seen, last_seen, self_text)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(hn_id) DO UPDATE SET title = excluded.title, last_seen = excluded.last_seen""",
            (story["hn_id"], story["title"], story.get("url", ""), story.get("domain", ""),
             posted or None, seen_at, seen_at, story.get("self_text", "")),
        )
        self.conn.execute(
            "INSERT INTO score_snapshots (hn_id, seen_at, score, rank, comments) VALUES (?, ?, ?, ?, ?)",
            (story["hn_id"], seen_at, story.get("score"), story.get("rank"), story.get("comments")),
        )
        self.conn.commit()
        return is_new

    def get_story(self, hn_id):
        row = self.conn.execute(
            """SELECT s.*, (SELECT score FROM score_snapshots WHERE hn_id = s.hn_id
                            ORDER BY seen_at DESC LIMIT 1) AS score
               FROM stories s WHERE hn_id = ?""", (hn_id,)).fetchone()
        return dict(row) if row else None

    def stories_needing_fetch(self, limit=50):
        """Stories with no article row yet (e.g. left over from an interrupted cycle)."""
        return [r["hn_id"] for r in self.conn.execute(
            """SELECT s.hn_id FROM stories s LEFT JOIN articles a USING (hn_id)
               WHERE a.hn_id IS NULL
               ORDER BY s.first_seen DESC,
                        (SELECT MAX(score) FROM score_snapshots WHERE hn_id = s.hn_id) DESC
               LIMIT ?""", (limit,))]

    def stories_needing_analysis(self, limit=50):
        """Stories whose current content has not been analysed (no analysis, or content changed)."""
        return [r["hn_id"] for r in self.conn.execute(
            """SELECT s.hn_id FROM stories s
               JOIN articles a USING (hn_id)
               LEFT JOIN analyses an USING (hn_id)
               WHERE an.hn_id IS NULL OR an.content_hash IS NOT a.content_hash
               ORDER BY s.first_seen DESC LIMIT ?""", (limit,))]

    # --- content -------------------------------------------------------------------------

    def save_article(self, hn_id, status, text="", final_url="", error="", method="none", readerable=None):
        text = text or ""
        self.conn.execute(
            """INSERT OR REPLACE INTO articles
               (hn_id, fetched_at, status, final_url, error, text, content_hash, word_count,
                extraction_method, readerable)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (hn_id, now_iso(), status, final_url, error, text, None, len(text.split()),
             method, None if readerable is None else int(bool(readerable))),
        )
        self.conn.commit()

    def save_comments(self, hn_id, comments):
        self.conn.execute("DELETE FROM comments WHERE hn_id = ?", (hn_id,))
        self.conn.executemany(
            "INSERT OR REPLACE INTO comments (comment_id, hn_id, position, author, text) VALUES (?, ?, ?, ?, ?)",
            [(c["comment_id"], hn_id, i, c.get("author", ""), c["text"]) for i, c in enumerate(comments)],
        )
        self.conn.commit()

    def finalize_content(self, hn_id):
        """Hash article + comments together so analysis can be skipped when nothing changed."""
        article = self.get_article(hn_id) or {}
        comments = "\n".join(c["text"] for c in self.get_comments(hn_id))
        digest = content_hash(article.get("text", ""), comments)
        self.conn.execute("UPDATE articles SET content_hash = ? WHERE hn_id = ?", (digest, hn_id))
        self.conn.commit()
        return digest

    def get_article(self, hn_id):
        row = self.conn.execute("SELECT * FROM articles WHERE hn_id = ?", (hn_id,)).fetchone()
        return dict(row) if row else None

    def get_comments(self, hn_id, limit=None):
        sql = "SELECT * FROM comments WHERE hn_id = ? ORDER BY position"
        params = (hn_id,)
        if limit is not None:
            sql += " LIMIT ?"
            params += (limit,)
        return [dict(r) for r in self.conn.execute(sql, params)]

    # --- analyses ------------------------------------------------------------------------

    def save_analysis(self, hn_id, analysis, model="", hash_="", prompt_tokens=0, completion_tokens=0):
        key_points = json.dumps(analysis.get("key_points", []))
        entities = analysis.get("entities", [])
        self.conn.execute(
            """INSERT OR REPLACE INTO analyses
               (hn_id, created_at, model, content_hash, summary, theme, sentiment, key_points, entities,
                prompt_tokens, completion_tokens)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (hn_id, now_iso(), model, hash_, analysis.get("summary", ""), analysis.get("theme", ""),
             analysis.get("sentiment", "neutral"), key_points, json.dumps(entities),
             prompt_tokens, completion_tokens),
        )
        self.conn.execute("DELETE FROM analyses_fts WHERE rowid = ?", (hn_id,))
        title = self.conn.execute("SELECT title FROM stories WHERE hn_id = ?", (hn_id,)).fetchone()
        self.conn.execute(
            "INSERT INTO analyses_fts (rowid, title, summary, theme, entities) VALUES (?, ?, ?, ?, ?)",
            (hn_id, title["title"] if title else "", analysis.get("summary", ""),
             analysis.get("theme", ""), " ".join(entities)),
        )
        self.conn.commit()

    def analyses_between(self, start, end):
        """Compact per-story view for the period (start, end]: what reports are built from (never raw text)."""
        rows = self.conn.execute(
            """SELECT an.hn_id, s.title, s.url, s.domain, an.created_at, an.summary, an.theme,
                      an.sentiment, an.key_points, an.entities,
                      (SELECT MAX(score) FROM score_snapshots WHERE hn_id = an.hn_id) AS score
               FROM analyses an JOIN stories s USING (hn_id)
               WHERE an.created_at > ? AND an.created_at <= ?
               ORDER BY score DESC""", (start, end))
        return [self._decode_analysis(r) for r in rows]

    def search(self, query, limit=10):
        rows = self.conn.execute(
            """SELECT an.hn_id, s.title, s.url, s.domain, an.created_at, an.summary, an.theme,
                      an.sentiment, an.key_points, an.entities, bm25(analyses_fts) AS rank
               FROM analyses_fts JOIN analyses an ON an.hn_id = analyses_fts.rowid
               JOIN stories s ON s.hn_id = an.hn_id
               WHERE analyses_fts MATCH ? ORDER BY rank LIMIT ?""", (query, limit))
        return [self._decode_analysis(r) for r in rows]

    def theme_stats(self, start, end):
        """Theme and sentiment counts computed in SQL, so reports spend no tokens on counting."""
        themes = self.conn.execute(
            """SELECT theme, COUNT(*) AS n FROM analyses WHERE created_at > ? AND created_at <= ?
               GROUP BY theme ORDER BY n DESC""", (start, end)).fetchall()
        sentiments = self.conn.execute(
            """SELECT sentiment, COUNT(*) AS n FROM analyses WHERE created_at > ? AND created_at <= ?
               GROUP BY sentiment ORDER BY n DESC""", (start, end)).fetchall()
        return {"themes": [(r["theme"], r["n"]) for r in themes],
                "sentiments": [(r["sentiment"], r["n"]) for r in sentiments]}

    @staticmethod
    def _decode_analysis(row):
        item = dict(row)
        for key in ("key_points", "entities"):
            try:
                item[key] = json.loads(item.get(key) or "[]")
            except (TypeError, ValueError):
                item[key] = []
        return item

    # --- reports & usage -----------------------------------------------------------------

    def save_report(self, period_start, period_end, story_count, content, prompt_tokens=0, completion_tokens=0):
        self.conn.execute(
            """INSERT INTO reports (created_at, period_start, period_end, story_count, content,
                                    prompt_tokens, completion_tokens) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (now_iso(), period_start, period_end, story_count, content, prompt_tokens, completion_tokens),
        )
        self.conn.commit()

    def last_report_time(self):
        row = self.conn.execute("SELECT MAX(created_at) AS t FROM reports").fetchone()
        return datetime.datetime.fromisoformat(row["t"]) if row and row["t"] else None

    def latest_report(self):
        row = self.conn.execute("SELECT * FROM reports ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def token_usage(self):
        row = self.conn.execute(
            """SELECT (SELECT COALESCE(SUM(prompt_tokens), 0) FROM analyses) +
                      (SELECT COALESCE(SUM(prompt_tokens), 0) FROM reports) AS prompt,
                      (SELECT COALESCE(SUM(completion_tokens), 0) FROM analyses) +
                      (SELECT COALESCE(SUM(completion_tokens), 0) FROM reports) AS completion""").fetchone()
        return {"prompt_tokens": row["prompt"], "completion_tokens": row["completion"]}

    # --- CSV interop ---------------------------------------------------------------------

    EXPORTS = {
        "stories": """SELECT s.hn_id, s.title, s.url, s.domain, s.posted_at, s.first_seen, s.last_seen,
                             (SELECT MAX(score) FROM score_snapshots WHERE hn_id = s.hn_id) AS max_score,
                             a.status AS fetch_status, a.word_count, a.extraction_method, a.readerable
                      FROM stories s LEFT JOIN articles a USING (hn_id) ORDER BY s.first_seen""",
        "insights": """SELECT an.created_at AS timestamp, an.hn_id, s.title, an.theme, an.sentiment,
                              an.summary, an.entities
                       FROM analyses an JOIN stories s USING (hn_id) ORDER BY an.created_at""",
        "reports": """SELECT created_at, period_start, period_end, story_count, content
                      FROM reports ORDER BY created_at""",
    }

    def export_csv(self, out_dir="."):
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        written = []
        for name, sql in self.EXPORTS.items():
            cursor = self.conn.execute(sql)
            path = out_dir / f"{name}.csv"
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([d[0] for d in cursor.description])
                writer.writerows(cursor)
            written.append(path)
        return written

    def import_legacy_csv(self, directory="."):
        """Import data written by the original CSV-based agent. Returns counts per file.

        Old seen_stories.csv rows have no HN id, so their titles are only used to skip
        re-analysing those stories (see is_legacy_seen); daily_reports.csv rows become reports.
        """
        directory = Path(directory)
        counts = {}
        seen = directory / "seen_stories.csv"
        if seen.exists() and seen.stat().st_size:
            with open(seen, encoding="utf-8") as f:
                rows = [(r["title"].strip(), r.get("first_seen")) for r in csv.DictReader(f) if r.get("title", "").strip()]
            self.conn.executemany("INSERT OR IGNORE INTO legacy_seen VALUES (?, ?)", rows)
            counts["seen_stories.csv"] = len(rows)
        reports = directory / "daily_reports.csv"
        if reports.exists() and reports.stat().st_size:
            with open(reports, encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            for r in rows:
                self.conn.execute(
                    "INSERT INTO reports (created_at, period_start, period_end, story_count, content) VALUES (?, ?, ?, ?, ?)",
                    (r.get("date"), r.get("date"), r.get("date"), int(r.get("insights_analyzed") or 0), r.get("summary", "")),
                )
            counts["daily_reports.csv"] = len(rows)
        self.conn.commit()
        return counts

    def is_legacy_seen(self, title):
        return self.conn.execute("SELECT 1 FROM legacy_seen WHERE title = ?", (title.strip(),)).fetchone() is not None
