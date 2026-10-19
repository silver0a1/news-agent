"""Flow nodes. The shared store carries handles (storage, llm, source, settings) and story IDs,
never bulk text: content lives in SQLite and is loaded per story when needed."""

import datetime
import time
from collections import Counter

from .browser import PageFetcher, PageResult
from .context import build_story_prompt, estimate_tokens, map_reduce, parse_analysis
from .pocketflow import Flow, Node


class CollectNode(Node):
    """Scan HN top stories, record every relevant one (with a score snapshot), queue new ones."""

    def prep(self, shared):
        print(f"[{datetime.datetime.now():%H:%M:%S}] 🔍 Scanning Hacker News top stories...")
        return shared["source"], shared["settings"].max_candidates

    def exec(self, prep_res):
        source, limit = prep_res
        return list(source.candidates(limit))

    def exec_fallback(self, prep_res, exc):
        print(f"❌ Collection failed: {exc}")
        return []

    def post(self, shared, prep_res, candidates):
        storage, settings = shared["storage"], shared["settings"]
        new = 0
        for story in candidates:
            if not storage.is_known(story["hn_id"]) and storage.is_legacy_seen(story["title"]):
                continue  # already analysed by the old CSV-based agent
            if storage.upsert_story(story):
                new += 1
                print(f"✨ New: {story['title'][:70]}")
        shared["candidates"] = {s["hn_id"]: s for s in candidates}
        # Includes stories left over from earlier cycles (over the per-cycle cap, or interrupted).
        shared["to_fetch"] = storage.stories_needing_fetch(limit=settings.max_new_per_cycle)
        backlog = storage.stories_needing_analysis(limit=1)
        print(f"📋 {len(candidates)} relevant, {new} new, {len(shared['to_fetch'])} queued for fetching")
        if shared["to_fetch"]:
            return "fetch"
        return "analyze" if backlog else "report"


class FetchNode(Node):
    """Browse each queued story's page with Playwright and pull its top HN comments."""

    def prep(self, shared):
        storage = shared["storage"]
        stories = [storage.get_story(i) for i in shared.get("to_fetch", [])]
        return {"stories": [s for s in stories if s], "source": shared["source"],
                "settings": shared["settings"], "candidates": shared.get("candidates", {}),
                "fetcher_factory": shared["fetcher_factory"]}

    def exec(self, ctx):
        stories, source, settings = ctx["stories"], ctx["source"], ctx["settings"]
        candidates = ctx["candidates"]
        results = {}

        for story in stories:
            kids = candidates.get(story["hn_id"], {}).get("kids")
            if kids is None:  # backlog story from an earlier cycle: refresh its comment ids
                try:
                    kids = (source.item(story["hn_id"]) or {}).get("kids", [])
                except Exception:
                    kids = []
            try:
                comments = source.comments(kids, settings.comments_limit)
            except Exception as e:
                print(f"⚠️  Comments failed for {story['hn_id']}: {e}")
                comments = []
            results[story["hn_id"]] = {"comments": comments, "page": None}

        to_browse = [s for s in stories if s.get("url")]
        if to_browse:
            try:
                with ctx["fetcher_factory"]() as fetcher:
                    for story in to_browse:
                        print(f"🌐 Fetching {story['url'][:80]}")
                        results[story["hn_id"]]["page"] = fetcher.fetch(story["url"])
            except Exception as e:  # browser failed to launch: analyse from title + comments
                print(f"❌ Browser unavailable: {e}")
                for story in to_browse:
                    if results[story["hn_id"]]["page"] is None:
                        results[story["hn_id"]]["page"] = PageResult(
                            url=story["url"], status="failed", error=f"browser: {e}"[:300])

        for story in stories:
            if not story.get("url"):  # Ask HN / Show HN self post: the text is the article
                results[story["hn_id"]]["page"] = PageResult(
                    url="", status="ok", text=story.get("self_text") or "", method="self_post")
        return results

    def post(self, shared, ctx, results):
        storage = shared["storage"]
        methods, fallback_reasons = Counter(), Counter()
        for hn_id, result in results.items():
            page = result["page"]
            method = page.method if page.status == "ok" and page.method else "none"
            methods[method] += 1
            if method == "fallback":
                fallback_reasons["not readerable" if page.readerable is False else "too short/failed"] += 1
            storage.save_article(hn_id, page.status, text=page.text, final_url=page.final_url,
                                 error=page.error, method=method, readerable=page.readerable)
            storage.save_comments(hn_id, result["comments"])
            storage.finalize_content(hn_id)
            words = len(page.text.split())
            print(f"💾 {hn_id}: page {page.status} via {method} ({words} words), {len(result['comments'])} comments"
                  + (f" — {page.error[:80]}" if page.error else ""))
        if results:
            print(extraction_summary(methods, fallback_reasons))
        return "analyze"


def extraction_summary(methods, fallback_reasons):
    """One line per cycle showing which extractor produced each article."""
    reasons = ", ".join(f"{r} {fallback_reasons[r]}" for r in ("not readerable", "too short/failed"))
    return (f"📑 Extraction: readability {methods['readability']} · fallback {methods['fallback']} "
            f"({reasons}) · self_post {methods['self_post']} · none {methods['none']}")


class AnalyzeNode(Node):
    """One LLM call per story; the compact result is stored once and reused by everything else."""

    def prep(self, shared):
        storage, settings = shared["storage"], shared["settings"]
        jobs = []
        for hn_id in storage.stories_needing_analysis(limit=settings.max_new_per_cycle * 2):
            story = storage.get_story(hn_id)
            article = storage.get_article(hn_id) or {}
            comments = storage.get_comments(hn_id, limit=settings.comments_limit)
            prompt = build_story_prompt(story, article.get("text", ""), comments,
                                        budget=settings.story_token_budget,
                                        article_char_limit=settings.article_char_limit)
            jobs.append({"hn_id": hn_id, "title": story["title"], "prompt": prompt,
                         "hash": article.get("content_hash")})
        print(f"🧠 Analysing {len(jobs)} stories (~{sum(estimate_tokens(j['prompt']) for j in jobs)} prompt tokens)")
        return shared["llm"], jobs

    def exec(self, prep_res):
        llm, jobs = prep_res
        done = []
        for job in jobs:
            usage = [0, 0]
            analysis = None
            for attempt in range(2):
                try:
                    result = llm.complete(job["prompt"], max_tokens=350, json_mode=True)
                except Exception as e:
                    # API problem: leave the story queued so the next cycle retries it.
                    print(f"⚠️  LLM call failed for {job['hn_id']}: {e}")
                    break
                usage[0] += result.prompt_tokens
                usage[1] += result.completion_tokens
                try:
                    analysis = parse_analysis(result.text)
                    break
                except ValueError as e:
                    print(f"⚠️  Unparseable analysis for {job['hn_id']} (attempt {attempt + 1}): {e}")
            else:
                # Two malformed replies: store a placeholder so we don't pay for this story forever.
                analysis = {"summary": job["title"], "theme": "Uncategorized", "sentiment": "neutral",
                            "key_points": [], "entities": []}
            if analysis:
                done.append((job, analysis, usage))
            if self.wait:
                time.sleep(self.wait)
        return done

    def post(self, shared, prep_res, done):
        storage, llm = shared["storage"], shared["llm"]
        for job, analysis, (p_tok, c_tok) in done:
            storage.save_analysis(job["hn_id"], analysis, model=llm.model, hash_=job["hash"],
                                  prompt_tokens=p_tok, completion_tokens=c_tok)
            print(f"✅ {analysis['theme']} ({analysis['sentiment']}): {job['title'][:60]}")
        return "report"


REPORT_MAP_PROMPT = """Condense these tech/AI news items into a digest of at most 150 words.
Group by theme, keep concrete names and numbers, drop anything trivial.

Items:
{items}"""

REPORT_REDUCE_PROMPT = """Write a briefing on tech/AI news for {period}.
Pre-computed statistics (exact, do not recount): {stats}

Using the material below, give:
1. The 2-3 most important developments and why they matter
2. Overall sentiment and how it shifted, if visible
3. One forward-looking prediction
Max 200 words. Plain text.

Material:
{{items}}"""


def generate_report(storage, llm, settings, start, end):
    """Build and save a report for (start, end]. Returns the saved report text, or None."""
    start_s, end_s = start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")
    items = storage.analyses_between(start_s, end_s)
    if not items:
        return None
    stats = storage.theme_stats(start_s, end_s)
    stats_line = ("themes " + ", ".join(f"{t} ×{n}" for t, n in stats["themes"][:8]) +
                  "; sentiment " + ", ".join(f"{s} ×{n}" for s, n in stats["sentiments"]) +
                  f"; {len(items)} stories")
    period = f"{start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M}"
    reduce_prompt = REPORT_REDUCE_PROMPT.format(
        period=period, stats=stats_line.replace("{", "{{").replace("}", "}}"))
    text, p_tok, c_tok = map_reduce(items, llm, settings.report_token_budget,
                                    REPORT_MAP_PROMPT, reduce_prompt)
    content = f"{period}\n{stats_line}\n\n{text}"
    storage.save_report(start_s, end_s, len(items), content, p_tok, c_tok)
    return content


class ReportNode(Node):
    """Produce a report when report_interval_hours have passed since the last one."""

    def prep(self, shared):
        storage, settings = shared["storage"], shared["settings"]
        now = datetime.datetime.now().replace(microsecond=0)
        last = storage.last_report_time()
        interval = datetime.timedelta(hours=settings.report_interval_hours)
        if last and now - last < interval:
            return None
        # Continue from where the previous report's period ended, so no story is missed or repeated.
        previous = storage.latest_report()
        try:
            start = datetime.datetime.fromisoformat(previous["period_end"])
        except (TypeError, ValueError):
            start = now - interval
        return storage, shared["llm"], settings, start, now

    def exec(self, prep_res):
        if not prep_res:
            return "not due"
        return generate_report(*prep_res) or "no analysed stories in period"

    def exec_fallback(self, prep_res, exc):
        return f"report failed: {exc}"

    def post(self, shared, prep_res, result):
        print(f"📊 Report: {result}")
        return "wait"


class WaitNode(Node):
    def prep(self, shared):
        return shared["settings"].interval_minutes

    def exec(self, minutes):
        print(f"⏳ Waiting {minutes:g} minutes before next cycle...")
        time.sleep(minutes * 60)

    def post(self, shared, prep_res, exec_res):
        return None


def create_agent_flow(once=False):
    collect = CollectNode(max_retries=2, wait=5)
    fetch = FetchNode()
    analyze = AnalyzeNode()
    report = ReportNode(max_retries=2, wait=3)

    collect - "fetch" >> fetch
    collect - "analyze" >> analyze
    collect - "report" >> report
    fetch - "analyze" >> analyze
    analyze - "report" >> report
    if not once:
        report - "wait" >> WaitNode()
    return Flow(start=collect)


def make_fetcher_factory(settings):
    return lambda: PageFetcher(timeout_s=settings.fetch_timeout_s, headless=settings.headless,
                               chromium_path=settings.chromium_path,
                               readability_min_chars=settings.readability_min_chars)
