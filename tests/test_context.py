import json

import pytest

from news_agent.context import (build_story_prompt, chunk, estimate_tokens, map_reduce, pack,
                                parse_analysis, truncate_to_tokens)
from news_agent.llm import FakeLLMClient


def item(i, summary="word " * 20):
    return {"hn_id": i, "title": f"Story {i}", "summary": summary, "theme": "AI",
            "sentiment": "neutral", "score": i, "entities": ["X"]}


def test_truncate_respects_budget():
    text = "Sentence one. " * 500
    out = truncate_to_tokens(text, 100)
    assert estimate_tokens(out) <= 105
    assert truncate_to_tokens("short", 100) == "short"


def test_story_prompt_fits_budget_and_keeps_comments():
    story = {"title": "T", "url": "https://x", "score": 5}
    comments = [{"text": "comment " * 50} for _ in range(30)]
    prompt = build_story_prompt(story, "article " * 20000, comments, budget=1500)
    assert estimate_tokens(prompt) <= 1600
    assert "Top HN comments" in prompt
    prompt = build_story_prompt(story, "", [], budget=1500)
    assert "article text unavailable" in prompt


def test_pack_and_chunk():
    items = [item(i) for i in range(50)]
    lines, overflow = pack(items, 300)
    assert lines and overflow and len(lines) + len(overflow) == 50
    assert sum(estimate_tokens(l) + 1 for l in lines) <= 300
    chunks = chunk(items, 300)
    assert sum(len(c) for c in chunks) == 50


def test_map_reduce_single_call_when_it_fits():
    llm = FakeLLMClient()
    text, p, c = map_reduce([item(1), item(2)], llm, 5000, "MAP {items}", "REDUCE {items}")
    assert len(llm.calls) == 1 and llm.calls[0].startswith("REDUCE")
    assert p > 0 and c > 0


def test_map_reduce_chunks_large_input():
    llm = FakeLLMClient()
    items = [item(i, summary="detail " * 80) for i in range(200)]
    map_reduce(items, llm, 2000, "MAP {items}", "REDUCE {items}")
    assert llm.calls[-1].startswith("REDUCE")
    assert sum(c.startswith("MAP") for c in llm.calls) > 1
    assert all(estimate_tokens(c) <= 2100 for c in llm.calls)


def test_parse_analysis_normalises():
    raw = "Here you go:\n" + json.dumps({"summary": "S", "theme": "", "sentiment": "Very Positive",
                                         "key_points": "one", "entities": ["a"] * 10})
    out = parse_analysis(raw)
    assert out["theme"] == "Uncategorized"
    assert out["sentiment"] == "neutral"
    assert out["key_points"] == ["one"] and len(out["entities"]) == 6
    with pytest.raises(ValueError):
        parse_analysis("no json here")
