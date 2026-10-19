"""Command line: python -m news_agent <command>."""

import argparse
import datetime
import os
import re
import time

from .config import Settings
from .llm import make_llm
from .nodes import create_agent_flow, generate_report, make_fetcher_factory
from .sources.hackernews import HackerNewsSource
from .storage import Storage


def parse_since(value):
    """'36h', '7d', '90m' -> timedelta."""
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([mhd])", value.strip().lower())
    if not match:
        raise argparse.ArgumentTypeError("use a number followed by m, h or d, e.g. 7d")
    amount, unit = float(match.group(1)), match.group(2)
    unit_name = {"m": "minutes", "h": "hours", "d": "days"}[unit]
    return datetime.timedelta(**{unit_name: amount})


def fts_query(text):
    """Turn free text into a safe FTS5 query: every word must match (prefix match on each)."""
    words = re.findall(r"\w+", text)
    return " ".join(f'"{w}"*' for w in words)


def build_shared(settings, storage):
    return {
        "settings": settings,
        "storage": storage,
        "llm": make_llm(settings),
        "source": HackerNewsSource(settings.keywords),
        "fetcher_factory": make_fetcher_factory(settings),
    }


def cmd_run(args, settings):
    if settings.llm_provider == "openai" and not os.getenv("OPENAI_API_KEY"):
        print("❌ Error: OPENAI_API_KEY environment variable not set (or use NEWS_AGENT_LLM=fake for a dry run)")
        return 1
    with Storage(settings.db_path) as storage:
        if not storage.get_meta("legacy_csv_imported"):
            counts = storage.import_legacy_csv(".")
            storage.set_meta("legacy_csv_imported", datetime.datetime.now().isoformat(timespec="seconds"))
            if counts:
                print(f"📥 Imported legacy CSV data: {counts}")
        shared = build_shared(settings, storage)
        agent = create_agent_flow(once=args.once)
        print(f"🤖 News agent started — db: {settings.db_path}, model: {shared['llm'].model}, "
              f"cycle: {settings.interval_minutes:g} min" + (" (single cycle)" if args.once else ""))
        cycles, started = 0, datetime.datetime.now()
        try:
            while True:
                print(f"\n🔄 Cycle {cycles + 1} — {datetime.datetime.now():%Y-%m-%d %H:%M:%S}")
                try:
                    agent.run(shared)
                    cycles += 1
                except Exception as e:
                    print(f"⚠️  Cycle error: {type(e).__name__}: {e}")
                    if args.once:
                        return 1
                    time.sleep(60)
                    continue
                if args.once:
                    break
        except KeyboardInterrupt:
            print(f"\n🛑 Stopped after {cycles} cycles, runtime {datetime.datetime.now() - started}")
        usage = storage.token_usage()
        print(f"🔢 Total LLM tokens so far: {usage['prompt_tokens']} prompt / {usage['completion_tokens']} completion")
    return 0


def cmd_report(args, settings):
    with Storage(settings.db_path) as storage:
        end = datetime.datetime.now()
        report = generate_report(storage, make_llm(settings), settings, end - args.since, end)
        print(report or "No analysed stories in that period.")
    return 0


def cmd_search(args, settings):
    query = fts_query(args.query)
    if not query:
        print("Empty query.")
        return 1
    with Storage(settings.db_path) as storage:
        results = storage.search(query, limit=args.limit)
        if not results:
            print("No matches.")
        for r in results:
            print(f"[{r['created_at'][:10]}] {r['theme']} ({r['sentiment']}) — {r['title']}")
            print(f"    {r['summary']}")
            print(f"    {r['url'] or 'https://news.ycombinator.com/item?id=' + str(r['hn_id'])}")
    return 0


def cmd_export(args, settings):
    with Storage(settings.db_path) as storage:
        for path in storage.export_csv(args.out):
            print(f"💾 Wrote {path}")
    return 0


def cmd_import(args, settings):
    with Storage(settings.db_path) as storage:
        print(storage.import_legacy_csv(args.dir) or "No legacy CSV files found.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="news_agent", description="Autonomous Hacker News monitoring agent")
    parser.add_argument("--db", help="SQLite database path (default: NEWS_AGENT_DB_PATH or news_agent.db)")
    parser.set_defaults(func=cmd_run, once=False)  # bare `python -m news_agent` runs the loop
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="run the monitoring loop (default)")
    run.add_argument("--once", action="store_true", help="run a single cycle and exit")
    run.set_defaults(func=cmd_run)

    report = sub.add_parser("report", help="generate a report now")
    report.add_argument("--since", type=parse_since, default=parse_since("1d"), help="period, e.g. 12h, 7d (default 1d)")
    report.set_defaults(func=cmd_report)

    search = sub.add_parser("search", help="full-text search over analysed stories")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=10)
    search.set_defaults(func=cmd_search)

    export = sub.add_parser("export-csv", help="export stories, insights and reports to CSV")
    export.add_argument("--out", default="exports")
    export.set_defaults(func=cmd_export)

    imp = sub.add_parser("import-csv", help="import CSV files written by the old version")
    imp.add_argument("--dir", default=".")
    imp.set_defaults(func=cmd_import)

    args = parser.parse_args(argv)
    settings = Settings.from_env()
    if args.db:
        settings.db_path = args.db
    return args.func(args, settings)
