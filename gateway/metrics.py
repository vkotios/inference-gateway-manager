"""Passive generation metrics read from proxied responses; never stores text."""

import json
import math
import time

MAX_LINE = 1024 * 1024
MAX_BODY = 4 * 1024 * 1024
LIVE_MIN_EVENTS = 3


def number(value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0 or (positive and value == 0):
        return None
    return value


def count(value):
    value = number(value)
    return int(value) if value is not None else None


def rate(value):
    value = number(value, positive=True)
    return round(value, 1) if value is not None else None


def has_content(delta):
    if not isinstance(delta, dict):
        return False
    return any(delta.get(key) for key in ("content", "reasoning_content", "tool_calls"))


class Tap:
    """Incremental reader for one response. Every failure disables it, silently."""

    def __init__(self, content_type=None, clock=time.monotonic):
        self.configure(content_type)
        self.clock = clock
        self.started = clock()
        self.events = 0
        self.first = None
        self.last = None
        self.timings = None
        self.usage = None
        self.buffer = bytearray()
        self.disabled = False

    def configure(self, content_type):
        self.stream = "text/event-stream" in (content_type or "").lower()

    def feed(self, chunk):
        if self.disabled:
            return
        try:
            self.buffer.extend(chunk)
            if self.stream:
                self.read_lines()
            elif len(self.buffer) > MAX_BODY:
                self.disabled = True
            if len(self.buffer) > MAX_LINE and self.stream:
                self.disabled = True
        except Exception:
            self.disabled = True
        if self.disabled:
            self.buffer.clear()

    def read_lines(self):
        while (end := self.buffer.find(b"\n")) >= 0:
            line = bytes(self.buffer[:end]).rstrip(b"\r")
            del self.buffer[: end + 1]
            if len(line) > MAX_LINE:
                self.disabled = True
                return
            if line.startswith(b"data:"):
                payload = line[5:].strip()
                if payload and payload != b"[DONE]":
                    self.read_event(payload)

    def read_event(self, payload):
        try:
            event = json.loads(payload)
        except (ValueError, RecursionError):
            return
        self.read_object(event, streaming=True)

    def read_object(self, event, streaming):
        if not isinstance(event, dict):
            return
        if isinstance(event.get("timings"), dict):
            self.timings = event["timings"]
        if isinstance(event.get("usage"), dict):
            self.usage = event["usage"]
        choices = event.get("choices")
        if streaming and isinstance(choices, list):
            if any(isinstance(c, dict) and has_content(c.get("delta")) for c in choices):
                now = self.clock()
                self.events += 1
                self.first = now if self.first is None else self.first
                self.last = now

    def finish_body(self):
        if self.stream or self.disabled or not self.buffer:
            return
        try:
            self.read_object(json.loads(bytes(self.buffer)), streaming=False)
        except (ValueError, RecursionError):
            pass

    def live(self):
        if self.disabled:
            return None
        rate_value = None
        if self.events >= LIVE_MIN_EVENTS and self.last > self.first:
            rate_value = rate((self.events - 1) / (self.last - self.first))
        return {
            "state": "generating" if self.events else "prefill",
            "elapsed_seconds": round(self.clock() - self.started, 1),
            "tokens": self.events,
            "tokens_per_second": rate_value,
        }

    def summary(self, status, outcome, *, model, client, now=None):
        if self.disabled or not 200 <= status < 300:
            return None
        self.finish_body()
        if self.disabled:
            return None
        timings = self.timings or {}
        usage = self.usage or {}
        generation_rate = rate(timings.get("predicted_per_second"))
        tokens = count(timings.get("predicted_n")) if generation_rate else None
        source = "engine" if generation_rate else "estimated"
        if tokens is None:
            tokens = count(usage.get("completion_tokens")) or self.events or None
        span = (self.last - self.first) if self.events >= 2 else 0
        if generation_rate is None and tokens and tokens > 1 and span > 0:
            generation_rate = rate((tokens - 1) / span)
        processed = count(timings.get("prompt_n"))
        prompt_rate = rate(timings.get("prompt_per_second")) if processed else None
        prefill_tokens = processed
        if prefill_tokens is None:
            prefill_tokens = count(usage.get("prompt_tokens"))
        proposed = count(timings.get("draft_n"))
        accepted = count(timings.get("draft_n_accepted"))
        draft = None
        if proposed and accepted is not None and accepted <= proposed:
            draft = {
                "proposed": proposed,
                "accepted": accepted,
                "acceptance": round(accepted / proposed, 3),
            }
        if not (tokens or generation_rate or prompt_rate):
            return None
        return {
            "finished_at": time.time() if now is None else now,
            "model": model,
            "client": client,
            "outcome": outcome,
            "source": source,
            "generation": {"tokens": tokens, "tokens_per_second": generation_rate},
            "prefill": {
                "tokens": prefill_tokens,
                "tokens_per_second": prompt_rate,
                "cached_tokens": count(timings.get("cache_n")),
            },
            "time_to_first_token_seconds": (
                round(self.first - self.started, 1) if self.first is not None else None
            ),
            "total_seconds": round(self.clock() - self.started, 1),
            "draft": draft,
        }


class Metrics:
    def __init__(self):
        self.last = None

    def record(self, summary):
        if summary is not None:
            self.last = summary

    def view(self, tap):
        return {"live": tap.live() if tap is not None else None, "last": self.last}
