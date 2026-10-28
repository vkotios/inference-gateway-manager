"""Optional read-only native phase/counter polling for buffered Lucebox responses."""

import asyncio
import json
import time

import httpx

from .metrics import MAX_BODY, count, number, rate

POLL_SECONDS = 1.0


class LuceboxMetrics:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = asyncio.Lock()
        self.key = None
        self.checked = float("-inf")
        self.cached = None
        self.previous = None

    async def live(self, client, base_url, tap, models):
        """Keep numeric counters only; never retain the endpoint's messages/excerpts."""
        async with self.lock:
            key = (base_url, id(tap), tap.started, tuple(models))
            if key != self.key:
                self.key, self.previous, self.cached = key, None, None
                self.checked = float("-inf")
            if self.clock() - self.checked < POLL_SECONDS:
                return self.cached
            self.checked = self.clock()
            try:
                # Do not follow redirects, buffer unbounded bodies, or block inference.
                async with client.stream(
                    "GET", base_url + "/status/json", timeout=0.5, follow_redirects=False
                ) as response:
                    if response.status_code != 200:
                        raise ValueError("native metrics unavailable")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_BODY:
                            raise ValueError("native metrics body too large")
                payload = json.loads(body)
                self.cached = self.parse(payload, tap, models)
            except (httpx.HTTPError, ValueError, TypeError, RecursionError):
                self.cached, self.previous = None, None
            return self.cached

    def parse(self, payload, tap, models):
        current = payload.get("current") if isinstance(payload, dict) else None
        if not isinstance(current, dict) or payload.get("phase") not in ("prefill", "decode"):
            self.previous = None
            return None
        elapsed = number(current.get("elapsed_s"))
        tokens = count(current.get("completion_tokens"))
        prompt = count(current.get("prompt_tokens"))
        if (
            current.get("model") not in models
            or elapsed is None
            or tokens is None
            or prompt is None
            or payload.get("active_requests", 0) != 0
            or elapsed > tap.clock() - tap.started + 1.0
        ):
            self.previous = None
            return None
        phase = payload["phase"]
        speed = None
        previous = self.previous
        if previous is not None and phase == previous[0] == "decode":
            _, prior_prompt, prior_elapsed, prior_tokens = previous
            span = elapsed - prior_elapsed
            if prompt == prior_prompt and span >= 0.5 and tokens >= prior_tokens:
                speed = rate((tokens - prior_tokens) / span)
        self.previous = (phase, prompt, elapsed, tokens)
        return {
            "state": "generating" if phase == "decode" else "prefill",
            "elapsed_seconds": round(tap.clock() - tap.started, 1),
            "tokens": tokens,
            "tokens_per_second": speed,
            "source": "engine",
        }
