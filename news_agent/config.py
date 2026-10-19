"""Runtime settings, loaded from NEWS_AGENT_* environment variables."""

import os
from dataclasses import dataclass

DEFAULT_KEYWORDS = (
    'ai', 'llm', 'llms', 'gpt', 'machine learning', 'artificial intelligence',
    'tech', 'startup', 'startups', 'programming', 'software', 'data', 'algorithm',
    'algorithms', 'openai', 'anthropic', 'claude', 'model', 'models', 'neural',
)


def _env(name, default, cast=str):
    value = os.getenv(f"NEWS_AGENT_{name}")
    if value is None or value == "":
        return default
    if cast is bool:
        return value.strip().lower() in ("1", "true", "yes", "on")
    return cast(value)


@dataclass
class Settings:
    db_path: str = "news_agent.db"
    interval_minutes: float = 15
    report_interval_hours: float = 24
    keywords: tuple = DEFAULT_KEYWORDS
    max_candidates: int = 60          # how many HN top-story IDs to scan
    max_new_per_cycle: int = 5        # how many new stories to fetch + analyze per cycle
    llm_provider: str = "openai"      # "openai" or "fake"
    model: str = "gpt-4o-mini"
    article_char_limit: int = 12000   # article text sent to the LLM (full text is still stored)
    comments_limit: int = 20          # top-level HN comments fetched per story
    story_token_budget: int = 3000    # prompt budget for a single story analysis
    report_token_budget: int = 6000   # prompt budget for one report map/reduce step
    fetch_timeout_s: float = 20
    headless: bool = True
    chromium_path: str = ""           # optional explicit Chromium executable
    readability_min_chars: int = 500  # shorter reader-mode output falls back to the heuristic extractor

    @classmethod
    def from_env(cls):
        keywords = _env("KEYWORDS", None)
        return cls(
            db_path=_env("DB_PATH", cls.db_path),
            interval_minutes=_env("INTERVAL_MINUTES", cls.interval_minutes, float),
            report_interval_hours=_env("REPORT_INTERVAL_HOURS", cls.report_interval_hours, float),
            keywords=tuple(k.strip().lower() for k in keywords.split(",") if k.strip()) if keywords else DEFAULT_KEYWORDS,
            max_candidates=_env("MAX_CANDIDATES", cls.max_candidates, int),
            max_new_per_cycle=_env("MAX_NEW_PER_CYCLE", cls.max_new_per_cycle, int),
            llm_provider=_env("LLM", cls.llm_provider),
            model=_env("MODEL", cls.model),
            article_char_limit=_env("ARTICLE_CHAR_LIMIT", cls.article_char_limit, int),
            comments_limit=_env("COMMENTS_LIMIT", cls.comments_limit, int),
            story_token_budget=_env("STORY_TOKEN_BUDGET", cls.story_token_budget, int),
            report_token_budget=_env("REPORT_TOKEN_BUDGET", cls.report_token_budget, int),
            fetch_timeout_s=_env("FETCH_TIMEOUT_S", cls.fetch_timeout_s, float),
            headless=_env("HEADLESS", cls.headless, bool),
            chromium_path=_env("CHROMIUM_PATH", cls.chromium_path),
            readability_min_chars=_env("READABILITY_MIN_CHARS", cls.readability_min_chars, int),
        )
