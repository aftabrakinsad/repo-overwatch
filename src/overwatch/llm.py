"""Thin wrappers around the Gemini and Claude APIs that always return parsed JSON.

Built to work on the Gemini free tier:
- RateLimiter paces requests and tokens per minute (free tiers allow only a few).
- Budget caps the number of AI requests in one run.
- A 429 is classified: per-minute limits are waited out and retried; daily quota
  (or a model with no free quota) raises QuotaExhausted so the run stops cleanly.
  Finished work is cached, so the next run continues where this one stopped.
"""
from __future__ import annotations

import json
import random
import re
import threading
import time
from collections import deque
from typing import Callable

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


class QuotaExhausted(RuntimeError):
    """No more AI requests are possible in this run (daily quota or per-run budget)."""


def parse_json(text: str | None) -> dict:
    cleaned = _FENCE.sub("", (text or "").strip())
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("the model did not return JSON") from None
        data = json.loads(cleaned[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("the model returned JSON that is not an object")
    return data


def classify_rate_limit(message: str) -> tuple[bool, float]:
    """For a 429/RESOURCE_EXHAUSTED error: (is it a daily/unavailable quota?, seconds to wait)."""
    daily = bool(re.search(r"per ?day|perday|daily|limit: ?0\b", message, re.I))
    wait = 0.0
    match = re.search(r"retry(?:[ _]?delay)?['\"]?\s*(?:in|[:=])?\s*['\"]?(\d+(?:\.\d+)?)\s*s", message, re.I)
    if match:
        wait = float(match.group(1))
    return daily, wait


def _is_rate_limit(exc: Exception) -> bool:
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    text = str(exc)
    return code == 429 or "RESOURCE_EXHAUSTED" in text or "429" in text[:40] or "rate_limit" in text.lower()


class Budget:
    """Caps AI requests per run across every model."""

    def __init__(self, max_requests: int):
        self.max_requests = max_requests
        self.used = 0
        self.stopped = ""
        self._lock = threading.Lock()

    def take(self) -> None:
        with self._lock:
            if self.stopped:
                raise QuotaExhausted(self.stopped)
            if self.max_requests and self.used >= self.max_requests:
                self.stopped = f"the per-run limit of {self.max_requests} AI requests (max_ai_requests) was reached"
                raise QuotaExhausted(self.stopped)
            self.used += 1

    def stop(self, reason: str) -> None:
        with self._lock:
            self.stopped = self.stopped or reason


class RateLimiter:
    """Sliding one-minute window over requests and (estimated) tokens for one model."""

    def __init__(self, rpm: int, tpm: int, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.rpm, self.tpm = rpm, tpm
        self._events: deque[tuple[float, int]] = deque()
        self._blocked_until = 0.0
        self._lock = threading.Lock()
        self._clock, self._sleep = clock, sleep

    def acquire(self, tokens: int) -> None:
        while True:
            with self._lock:
                now = self._clock()
                while self._events and now - self._events[0][0] >= 60:
                    self._events.popleft()
                tokens = min(tokens, self.tpm) if self.tpm else tokens
                used = sum(t for _, t in self._events)
                window_full = (self.rpm and len(self._events) >= self.rpm) or (
                    self.tpm and self._events and used + tokens > self.tpm
                )
                if now < self._blocked_until:
                    wait = self._blocked_until - now  # told to back off after a 429
                elif window_full:
                    wait = 60 - (now - self._events[0][0])  # until the oldest request leaves the window
                else:
                    self._events.append((now, tokens))
                    return
            self._sleep(min(max(wait, 0.5) + 0.25, 65))

    def block(self, seconds: float) -> None:
        with self._lock:
            self._blocked_until = max(self._blocked_until, self._clock() + seconds)


def _with_retries(call: Callable[[], dict], what: str, attempts: int = 5) -> dict:
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return call()
        except QuotaExhausted:
            raise
        except Exception as exc:  # network errors, per-minute limits, malformed JSON
            last = exc
            if attempt < attempts - 1:
                time.sleep(min(60, 4 * 2**attempt) + random.uniform(0, 2))
    raise RuntimeError(f"{what} failed after {attempts} attempts: {last}") from last


class Usage:
    def __init__(self) -> None:
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self._lock = threading.Lock()

    def add(self, inp: int | None, out: int | None) -> None:
        with self._lock:
            self.calls += 1
            self.input_tokens += inp or 0
            self.output_tokens += out or 0


class GeminiClient:
    def __init__(self, api_key: str, model: str, limiter: RateLimiter | None = None, budget: Budget | None = None):
        from google import genai
        from google.genai import types

        self._types = types
        self.model = model
        self.client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=600_000))
        self.limiter = limiter or RateLimiter(0, 0)
        self.budget = budget or Budget(0)
        self.usage = Usage()

    def json(self, system: str, prompt: str, max_output_tokens: int = 65_536) -> dict:
        estimated_tokens = (len(system) + len(prompt)) // 4 + 2_000

        def call() -> dict:
            self.limiter.acquire(estimated_tokens)
            self.budget.take()
            try:
                response = self.client.models.generate_content(
                    model=self.model,
                    contents=prompt,
                    config=self._types.GenerateContentConfig(
                        system_instruction=system,
                        response_mime_type="application/json",
                        max_output_tokens=max_output_tokens,
                    ),
                )
            except Exception as exc:
                if _is_rate_limit(exc):
                    daily, wait = classify_rate_limit(str(exc))
                    if daily:
                        reason = (
                            f"the Gemini quota for `{self.model}` is used up for today "
                            "(free-tier daily quotas reset at midnight Pacific time)"
                        )
                        self.budget.stop(reason)
                        raise QuotaExhausted(reason) from exc
                    self.limiter.block(wait or 30)  # per-minute limit: pause everyone, then retry
                raise
            meta = getattr(response, "usage_metadata", None)
            self.usage.add(getattr(meta, "prompt_token_count", 0), getattr(meta, "candidates_token_count", 0))
            return parse_json(response.text)

        return _with_retries(call, f"Gemini ({self.model})")


class ClaudeClient:
    def __init__(self, api_key: str, model: str, budget: Budget | None = None):
        import anthropic

        self.model = model
        self.client = anthropic.Anthropic(api_key=api_key, max_retries=3, timeout=900)
        self.budget = budget or Budget(0)
        self.usage = Usage()

    def json(self, system: str, prompt: str, max_tokens: int = 32_000) -> dict:
        def call() -> dict:
            self.budget.take()
            with self.client.messages.stream(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                message = stream.get_final_message()
            self.usage.add(message.usage.input_tokens, message.usage.output_tokens)
            text = "".join(block.text for block in message.content if block.type == "text")
            return parse_json(text)

        return _with_retries(call, f"Claude ({self.model})")
