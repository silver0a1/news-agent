"""LLM provider interface. Swap providers by implementing LLMClient."""

import json
import os
from dataclasses import dataclass
from typing import Protocol


@dataclass
class LLMResult:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0


class LLMClient(Protocol):
    model: str

    def complete(self, prompt: str, *, max_tokens: int = 300, json_mode: bool = False) -> LLMResult:
        """Return the model's reply. Raises on failure so Node retries/fallbacks can trigger."""
        ...


class OpenAIClient:
    def __init__(self, model="gpt-4o-mini", api_key=None, temperature=0.3):
        from openai import OpenAI
        self.model = model
        self.temperature = temperature
        self._client = OpenAI(api_key=api_key or os.getenv("OPENAI_API_KEY"))

    def complete(self, prompt, *, max_tokens=300, json_mode=False):
        kwargs = {"response_format": {"type": "json_object"}} if json_mode else {}
        response = self._client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=self.temperature,
            **kwargs,
        )
        usage = response.usage
        return LLMResult(
            text=(response.choices[0].message.content or "").strip(),
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
        )


class FakeLLMClient:
    """Deterministic offline client for tests and dry runs (NEWS_AGENT_LLM=fake)."""

    model = "fake"

    def __init__(self):
        self.calls = []

    def complete(self, prompt, *, max_tokens=300, json_mode=False):
        self.calls.append(prompt)
        if json_mode:
            text = json.dumps({
                "summary": f"Fake summary of a {len(prompt)}-char prompt.",
                "theme": "Testing",
                "sentiment": "neutral",
                "key_points": ["fake point"],
                "entities": ["FakeCorp"],
            })
        else:
            text = f"Fake digest ({len(prompt)} chars of input)."
        return LLMResult(text=text, prompt_tokens=len(prompt) // 4, completion_tokens=len(text) // 4)


def make_llm(settings) -> LLMClient:
    if settings.llm_provider == "fake":
        return FakeLLMClient()
    if settings.llm_provider == "openai":
        return OpenAIClient(model=settings.model)
    raise ValueError(f"Unknown LLM provider: {settings.llm_provider!r}")
