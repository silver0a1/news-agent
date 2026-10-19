# What It Does

Imagine you want to stay on top of tech and AI news trends, but you don't have time to constantly check Hacker News, read the articles, skim the discussions, and track patterns. This agent is like having 24/7 digital eyes that:

1. Monitors Hacker News https://news.ycombinator.com for you around the clock
2. Focuses on stories matching your chosen topics (AI, programming, startups, tech by default)
3. Opens each new story's linked page in a headless browser and reads the article, plus the top HN comments
4. Writes a compact analysis per story: summary, theme, sentiment, key points, and named entities
5. Stores everything in a local SQLite database that you can search and export to spreadsheets
6. Generates daily (or on-demand weekly) briefings without ever re-reading raw articles

---

## Quick Start

```bash
pip install -r requirements.txt
playwright install chromium          # one-time browser download
export OPENAI_API_KEY=sk-...

python -m news_agent run             # run forever (same as: python main.py)
python -m news_agent run --once      # one cycle, then exit
NEWS_AGENT_LLM=fake python -m news_agent run --once   # dry run, no API key or cost
```

Other commands:

| Command | What it does |
|---|---|
| `python -m news_agent report --since 7d` | Build a briefing for any period now (e.g. `12h`, `7d`) |
| `python -m news_agent search "open source models"` | Full-text search over every analysed story |
| `python -m news_agent export-csv --out exports` | Write `stories.csv`, `insights.csv`, `reports.csv` for Excel |
| `python -m news_agent import-csv` | Import CSV files from the original version (also done automatically on first run) |

Run the tests with `pytest`.

---

##  What Happens Every Cycle (15 minutes by default)

### Step 1: News Hunting
- Scans the top 60 Hacker News stories
- Smart filtering: whole-word keyword matching, so "AI" matches "AI agents" but not "said" or "rain"
- Records every relevant story's score and rank on each pass, so the database shows how stories trend over time

### Step 2: Duplicate Detection
- Tracks stories by their Hacker News ID, so a story is processed once even if its title is edited
- New stories over the per-cycle cap (5) wait in a queue for the next cycle, highest-scoring first
- If the agent is stopped mid-cycle, it picks up unfinished stories when restarted

### Step 3: Browsing
- Opens each new story's link in headless Chromium (Playwright), skipping images, fonts and media for speed
- Extracts the main article text and strips navigation, sidebars and footers
- Pulls the top 20 HN comments for the community's take
- Ask HN / Show HN posts use the post text itself; if a page can't be loaded, the story is still analysed from its title and comments

### Step 4: AI Analysis (one call per story)
- Sends a token-budgeted prompt (article excerpt + top comments) to the model
- Gets structured JSON back, for example:
  - Theme: "AI Healthcare"
  - Sentiment: "positive"
  - Summary: "Medical AI showing promising results in early diagnosis..."
  - Entities: ["Google", "Med-Gemini"]
- A story is only analysed again if its fetched content actually changes

### Step 5: Reports
- Once every 24 hours, the agent writes a briefing covering everything analysed since the last one
- Theme and sentiment counts are calculated by the database, not the AI, so they cost nothing and are exact

### Step 6: Wait Period
- Waits before the next cycle (configurable) to keep API costs down and respect Hacker News's servers

---

## Keeping Context Small as Data Grows

The agent is built so prompts stay the same size whether the database holds 50 stories or 50,000:

- **Read once, summarise once.** Raw article text is analysed a single time per story. Everything after that, such as reports and search, works from the compact per-story analysis rows.
- **Token budgets everywhere.** Each story prompt is assembled against a fixed budget (`NEWS_AGENT_STORY_TOKEN_BUDGET`). The article excerpt and comments are trimmed at sentence boundaries to fit.
- **Map-reduce reports.** If a period's analyses don't fit into one prompt, they are digested in chunks and the digests are combined into the final briefing.
- **Retrieval instead of recall.** The SQLite full-text index (FTS5) finds relevant stories on demand, so nothing has to be kept in the model's context.
- **Counting in SQL.** Statistics are computed in the database and handed to the model as facts.

---

##  Where Data Lives

Everything is stored in `news_agent.db` (SQLite):

| Table | Contents |
|---|---|
| `stories` | One row per HN story: id, title, URL, domain, first/last seen |
| `score_snapshots` | Score, rank and comment count each time the story was seen |
| `articles` | Fetched page text, fetch status/errors, content hash, word count |
| `comments` | Top HN comments per story |
| `analyses` | Summary, theme, sentiment, key points, entities, tokens used |
| `analyses_fts` | Full-text search index over titles, summaries, themes, entities |
| `reports` | Generated briefings with their period and token cost |

You can open it with any SQLite tool, or use `export-csv` to get spreadsheet files:

### `insights.csv` - Your Daily Intelligence
```
timestamp,hn_id,title,theme,sentiment,summary,entities
2025-07-18T10:16:28,44612345,"New model for early diagnosis",AI Healthcare,positive,"Medical AI showing...","[""Google""]"
```

### `stories.csv` - The Agent's Memory
Every story ever seen, with its peak score and whether its page was fetched.

### `reports.csv` - Executive Summaries
Every briefing, with the period it covers.

Upgrading from the CSV version? On first run, the agent imports `seen_stories.csv` (so those stories aren't re-analysed) and `daily_reports.csv`.

---

## Configuration

All settings are optional environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `NEWS_AGENT_DB_PATH` | `news_agent.db` | Database file (or pass `--db`) |
| `NEWS_AGENT_INTERVAL_MINUTES` | `15` | Wait between cycles |
| `NEWS_AGENT_REPORT_INTERVAL_HOURS` | `24` | How often a report is generated |
| `NEWS_AGENT_KEYWORDS` | AI/tech list | Comma-separated topics to follow |
| `NEWS_AGENT_MAX_CANDIDATES` | `60` | Top stories scanned per cycle |
| `NEWS_AGENT_MAX_NEW_PER_CYCLE` | `5` | Stories fetched and analysed per cycle |
| `NEWS_AGENT_LLM` | `openai` | `openai`, or `fake` for offline dry runs |
| `NEWS_AGENT_MODEL` | `gpt-4o-mini` | Model name |
| `NEWS_AGENT_ARTICLE_CHAR_LIMIT` | `12000` | Article characters considered per story |
| `NEWS_AGENT_COMMENTS_LIMIT` | `20` | HN comments fetched per story |
| `NEWS_AGENT_STORY_TOKEN_BUDGET` | `3000` | Prompt budget per story |
| `NEWS_AGENT_REPORT_TOKEN_BUDGET` | `6000` | Prompt budget per report step |
| `NEWS_AGENT_FETCH_TIMEOUT_S` | `20` | Page load timeout |
| `NEWS_AGENT_HEADLESS` | `true` | Set `false` to watch the browser |
| `NEWS_AGENT_CHROMIUM_PATH` | (Playwright's) | Use a specific Chromium binary |

`HTTPS_PROXY` / `NO_PROXY` are passed through to the browser.

---

## Project Layout

```
news_agent/
  pocketflow.py        # the 100-line PocketFlow framework
  config.py            # settings from environment variables
  llm.py               # LLMClient interface, OpenAI + fake clients
  sources/hackernews.py
  browser.py           # Playwright page fetching + article extraction
  storage.py           # SQLite schema and queries, CSV import/export
  context.py           # token budgeting, packing, map-reduce
  nodes.py             # Collect -> Fetch -> Analyze -> Report -> Wait
  cli.py
tests/                 # pytest suite (no network or API key needed)
```

To use a different AI provider, implement the `complete()` method from `LLMClient` in `llm.py` and register the provider in `make_llm`.

---

##  Key Features

### Runs Forever Without You
- Set and forget: Start it once, runs until you stop it
- Self-healing: If something breaks, it tries again automatically
- No babysitting: Doesn't need you to click buttons or check on it

### Smart Cost Management
- Avoids waste: Never analyses the same story twice unless its content changes
- Bounded prompts: Token budgets keep each call small no matter how much data has accumulated
- Budget-conscious: Uses a low-cost model by default and records every token used

### Handles Problems Gracefully
- Internet down? Waits and tries again
- Page won't load? Analyses the story from its title and HN discussion
- AI service down? Leaves stories queued and retries them next cycle
- Power outage? Picks up unfinished stories when restarted

---

##  Real-World Value

### For Business Intelligence
- Market awareness: Know what's trending in your industry
- Competitive intelligence: Track what competitors are doing
- Investment insights: Spot emerging technologies early
- Trend analysis: Historical data shows what's gaining/losing momentum

### For Cost Efficiency
- Replaces manual work: No need to hire someone to monitor news
- Cheaper than subscriptions: Costs less than most news analysis services
- Scalable: Could easily monitor multiple sources or topics

### For Decision Making
- Data-driven insights: Decisions based on actual trend analysis, not gut feeling
- Historical context: See how current news compares to past patterns
- Sentiment tracking: Understand if the tech community is optimistic or pessimistic

---

##  Example: A Week of Operation

Monday: Agent finds 4 AI stories, analyzes "AI Healthcare" trend as positive  
Tuesday: Same stories still trending, agent skips them (saves $0.50 in API costs)  
Wednesday: New breakthrough story appears, agent analyzes "Quantum Computing" as positive  
Thursday: Mixed stories, agent identifies "Regulation Concerns" as negative  
Friday: Funding news, agent spots "Startup Investment" as positive  
Weekend: Slower news, agent mostly skips duplicates  

Weekly Report Generated: "Key trends: Healthcare AI and quantum computing gaining momentum. Regulatory concerns emerging. Overall positive sentiment in startup funding."

---

##  Who This Is For

### Tech Executives
- Stay informed without information overload
- Get AI-powered insights instead of raw news dumps
- Track industry sentiment and emerging trends

### Investors & VCs
- Spot emerging technologies before they go mainstream
- Monitor startup ecosystem health
- Track sentiment around investment themes

### Researchers & Analysts
- Automated data collection for trend analysis
- Historical database of tech developments
- Quantified sentiment analysis over time

### Anyone Curious About Tech
- Effortless way to stay current with tech trends
- See the "big picture" of what's happening
- Learn what matters without drowning in noise

---

You start it once, and from that moment forward, you have a continuously updating database of tech intelligence that most companies would pay thousands for. It's automation that actually works - set it and forget it, then check your insights whenever you want to make informed decisions.


## Free Usage

If you need free usage of an AI service, OpenAI allows a certain amount of free calls (2.5 million for some models) per day if you share your data with them. When you pay, it is for privacy or performance.
