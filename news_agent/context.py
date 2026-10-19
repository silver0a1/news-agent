"""Keeping LLM context small as the dataset grows.

Strategy:
  1. Raw page text is read once per story and condensed into a compact analysis row
     (summary, theme, sentiment, key points, entities). Everything downstream works from
     those rows, never from raw articles.
  2. Each prompt is assembled against an explicit token budget (`fit_sections`, `pack`).
  3. When a period's analyses don't fit one prompt, `map_reduce` digests them in chunks
     and then combines the digests.
  4. Counting (theme/sentiment totals) is done in SQL, not by the LLM.
"""

import json
import re

SENTIMENTS = ("positive", "negative", "neutral", "mixed")


def estimate_tokens(text):
    """Rough token count (~4 chars/token for English). Good enough for budgeting."""
    return (len(text or "") + 3) // 4


def truncate_to_tokens(text, max_tokens):
    text = text or ""
    max_chars = max(0, max_tokens) * 4
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    # Prefer ending on a paragraph or sentence boundary.
    for sep in ("\n\n", ". ", "\n"):
        idx = cut.rfind(sep)
        if idx > max_chars * 0.6:
            cut = cut[: idx + len(sep)]
            break
    return cut.rstrip() + " […]"


def fit_sections(sections, budget):
    """Fill `budget` tokens with sections in priority order.

    `sections` is a list of (text, share) where share is the fraction of the remaining budget
    a section may use; leftover budget from short sections flows to later ones.
    """
    out, remaining = [], budget
    for i, (text, share) in enumerate(sections):
        if not text or remaining <= 0:
            continue
        is_last = i == len(sections) - 1
        allowance = remaining if is_last else max(1, int(remaining * share))
        piece = truncate_to_tokens(text, allowance)
        out.append(piece)
        remaining -= estimate_tokens(piece)
    return out


STORY_PROMPT = """You are analysing one Hacker News story for a tech/AI trend tracker.
Use the article text and discussion below. Respond with JSON only, using exactly these keys:
  "summary":    2-3 sentences, max 60 words, what happened and why it matters
  "theme":      1-3 word topic label (e.g. "AI Agents", "Chip Supply", "Open Source")
  "sentiment":  one of "positive", "negative", "neutral", "mixed" (tone of the news + discussion)
  "key_points": up to 3 short strings
  "entities":   up to 6 company, product, person or technology names

"""


def build_story_prompt(story, article_text, comments, budget, article_char_limit=12000):
    header = (f"Title: {story['title']}\nURL: {story.get('url') or '(self post)'}\n"
              f"Score: {story.get('score', 0)}\n")
    article = (article_text or "")[:article_char_limit]
    discussion = "\n".join(f"- {c['text']}" for c in comments)
    body_budget = budget - estimate_tokens(STORY_PROMPT + header)
    article_part, comment_part = "", ""
    parts = fit_sections([(article, 0.7), (discussion, 1.0)], body_budget)
    if article and parts:
        article_part = parts.pop(0)
    if discussion and parts:
        comment_part = parts.pop(0)
    prompt = STORY_PROMPT + header
    prompt += f"\nArticle:\n{article_part or '(article text unavailable; rely on title and discussion)'}\n"
    if comment_part:
        prompt += f"\nTop HN comments:\n{comment_part}\n"
    return prompt


def parse_analysis(text):
    """Parse and normalise the per-story JSON. Raises ValueError if it can't be salvaged."""
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not match:
        raise ValueError(f"No JSON object in LLM reply: {text[:120]!r}")
    data = json.loads(match.group(0))
    sentiment = str(data.get("sentiment", "neutral")).strip().lower()

    def as_list(value, limit):
        if isinstance(value, str):
            value = [value]
        return [str(v).strip() for v in (value or []) if str(v).strip()][:limit]

    return {
        "summary": str(data.get("summary", "")).strip(),
        "theme": str(data.get("theme", "")).strip() or "Uncategorized",
        "sentiment": sentiment if sentiment in SENTIMENTS else "neutral",
        "key_points": as_list(data.get("key_points"), 3),
        "entities": as_list(data.get("entities"), 6),
    }


def render_analysis(item):
    """One compact line per story: the unit reports are built from."""
    entities = ", ".join(item.get("entities") or [])
    line = f"- [{item.get('theme', '')} | {item.get('sentiment', '')} | score {item.get('score') or 0}] {item['title']}: {item.get('summary', '')}"
    return line + (f" (entities: {entities})" if entities else "")


def pack(items, budget, render=render_analysis):
    """Greedily take items (already in priority order) until the token budget is used.

    Returns (packed_lines, overflow_items).
    """
    lines, used = [], 0
    for i, item in enumerate(items):
        line = render(item)
        cost = estimate_tokens(line) + 1
        if used + cost > budget and lines:
            return lines, items[i:]
        lines.append(line if cost <= budget else truncate_to_tokens(line, budget))
        used += cost
    return lines, []


def chunk(items, budget, render=render_analysis):
    """Split items into consecutive chunks that each fit in `budget` tokens."""
    chunks, rest = [], list(items)
    while rest:
        lines, rest = pack(rest, budget, render)
        chunks.append(lines)
    return chunks


def map_reduce(items, llm, budget, map_prompt, reduce_prompt, render=render_analysis, max_tokens=400):
    """Digest `items` in budget-sized chunks, then merge the digests.

    `map_prompt` / `reduce_prompt` are format strings with an `{items}` placeholder.
    Returns (text, prompt_tokens, completion_tokens). Reduces recursively if the digests
    themselves overflow the budget.
    """
    usage = [0, 0]

    def call(prompt):
        result = llm.complete(prompt, max_tokens=max_tokens)
        usage[0] += result.prompt_tokens
        usage[1] += result.completion_tokens
        return result.text

    overhead = max(estimate_tokens(map_prompt), estimate_tokens(reduce_prompt))
    # Room for at least two digests per chunk, so each reduce round at least halves the count.
    room = max(2 * max_tokens + 50, budget - overhead)
    chunks = chunk(items, room, render)
    if len(chunks) == 1:
        return call(reduce_prompt.format(items="\n".join(chunks[0]))), usage[0], usage[1]

    digests = [call(map_prompt.format(items="\n".join(lines))) for lines in chunks]
    while True:
        digest_chunks = chunk(digests, room, render=lambda d: d)
        if len(digest_chunks) == 1:
            break
        digests = [call(map_prompt.format(items="\n\n".join(c))) for c in digest_chunks]
    final = call(reduce_prompt.format(items="\n\n".join(digest_chunks[0])))
    return final, usage[0], usage[1]
