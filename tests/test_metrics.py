"""Generation metrics tap: passive, content-free and honest about what is measured."""

import json

from gateway.metrics import Metrics, Tap

TIMINGS = {
    "prompt_n": 2048,
    "prompt_per_second": 173.0,
    "cache_n": 12,
    "predicted_n": 1219,
    "predicted_per_second": 25.6,
    "draft_n": 1578,
    "draft_n_accepted": 990,
}


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def sse(*events):
    return b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events) + (
        b"data: [DONE]\n\n"
    )


def token(text="x"):
    return {"choices": [{"delta": {"content": text}}]}


def final(**extra):
    return {"choices": [{"delta": {}, "finish_reason": "stop"}], **extra}


def make(content_type="text/event-stream"):
    clock = Clock()
    return Tap(content_type, clock=clock), clock


def finish(tap, status=200, outcome="complete"):
    return tap.summary(status, outcome, model="m", client="c", now=1000.0)


def test_engine_timings_give_exact_generation_and_prefill():
    tap, clock = make()
    tap.feed(sse({"choices": [{"delta": {"role": "assistant", "content": ""}}]}))
    clock.now += 11.9
    tap.feed(sse(token("SECRET-TEXT")))
    clock.now += 1.0
    tap.feed(sse(final(timings=TIMINGS)))
    s = finish(tap)
    assert s["source"] == "engine" and s["outcome"] == "complete"
    assert s["generation"] == {"tokens": 1219, "tokens_per_second": 25.6}
    assert s["prefill"] == {"tokens": 2048, "tokens_per_second": 173.0, "cached_tokens": 12}
    assert s["time_to_first_token_seconds"] == 11.9
    assert s["draft"] == {"proposed": 1578, "accepted": 990, "acceptance": 0.627}
    assert s["model"] == "m" and s["client"] == "c" and s["finished_at"] == 1000.0
    assert "SECRET-TEXT" not in json.dumps(s)


def test_without_timings_generation_is_estimated_and_prefill_is_unknown():
    tap, clock = make()
    for _ in range(11):
        tap.feed(sse(token()))
        clock.now += 0.1
    s = finish(tap)
    assert s["source"] == "estimated"
    assert s["generation"]["tokens"] == 11
    # 10 intervals of 0.1 s between first and last content event.
    assert s["generation"]["tokens_per_second"] == 10.0
    assert s["prefill"] == {"tokens": None, "tokens_per_second": None, "cached_tokens": None}
    assert s["draft"] is None


def test_usage_supplies_token_count_when_timings_are_absent():
    tap, clock = make()
    tap.feed(sse(token()))
    clock.now += 2.0
    tap.feed(sse(token(), final(usage={"prompt_tokens": 50, "completion_tokens": 40})))
    s = finish(tap)
    assert s["source"] == "estimated"
    assert s["generation"]["tokens"] == 40
    assert s["prefill"]["tokens"] == 50 and s["prefill"]["tokens_per_second"] is None


def test_events_split_across_chunks_and_crlf_are_parsed():
    tap, clock = make()
    raw = sse(token("a"), final(timings=TIMINGS)).replace(b"\n", b"\r\n")
    for i in range(0, len(raw), 7):
        tap.feed(raw[i : i + 7])
    s = finish(tap)
    assert s["source"] == "engine" and s["generation"]["tokens"] == 1219


def test_reasoning_and_tool_call_deltas_count_as_content():
    tap, clock = make()
    tap.feed(sse({"choices": [{"delta": {"reasoning_content": "think"}}]}))
    clock.now += 1
    tap.feed(sse({"choices": [{"delta": {"tool_calls": [{"function": {"arguments": "{"}}]}}]}))
    clock.now += 1
    tap.feed(sse({"choices": [{"delta": {"content": ""}}]}))
    assert finish(tap)["generation"]["tokens"] == 2


def test_live_view_waits_for_three_events():
    tap, clock = make()
    assert tap.live() == {
        "state": "prefill",
        "elapsed_seconds": 0.0,
        "tokens": 0,
        "tokens_per_second": None,
    }
    clock.now += 4.0
    tap.feed(sse(token()))
    clock.now += 0.5
    tap.feed(sse(token()))
    live = tap.live()
    assert live["state"] == "generating" and live["tokens"] == 2
    assert live["tokens_per_second"] is None
    clock.now += 0.5
    tap.feed(sse(token()))
    live = tap.live()
    assert live["tokens"] == 3 and live["tokens_per_second"] == 2.0
    assert live["elapsed_seconds"] == 5.0


def test_plain_json_body_uses_timings_and_usage():
    tap, clock = make("application/json")
    body = json.dumps(
        {"choices": [], "usage": {"completion_tokens": 7}, "timings": TIMINGS}
    ).encode()
    tap.feed(body[:20])
    tap.feed(body[20:])
    s = finish(tap)
    assert s["source"] == "engine" and s["generation"]["tokens"] == 1219


def test_non_generation_responses_record_nothing():
    for content_type, body in (
        ("application/json", b'{"data": [{"embedding": [0.1]}]}'),
        ("application/json", b'{"object": "list"}'),
        ("text/event-stream", b"data: [DONE]\n\n"),
        ("application/json", b"not json"),
    ):
        tap, _ = make(content_type)
        tap.feed(body)
        assert finish(tap) is None
    tap, _ = make()
    tap.feed(sse(token(), final(timings=TIMINGS)))
    assert finish(tap, status=500) is None


def test_cancelled_stream_is_recorded_as_cancelled_estimate():
    tap, clock = make()
    for _ in range(4):
        tap.feed(sse(token()))
        clock.now += 0.5
    s = finish(tap, outcome="cancelled")
    assert s["outcome"] == "cancelled" and s["source"] == "estimated"
    assert s["generation"]["tokens"] == 4


def test_invalid_timing_values_are_ignored_not_invented():
    bad = {
        "prompt_n": -1,
        "prompt_per_second": float("nan"),
        "predicted_n": True,
        "predicted_per_second": "fast",
        "draft_n": 0,
        "draft_n_accepted": 0,
    }
    tap, clock = make()
    tap.feed(sse(token()))
    clock.now += 1
    tap.feed(sse(final(timings=bad)))
    s = finish(tap)
    assert s["generation"]["tokens_per_second"] is None
    assert s["prefill"]["tokens"] is None and s["prefill"]["tokens_per_second"] is None
    assert s["draft"] is None


def test_zero_processed_prompt_reports_no_prefill_speed():
    tap, _ = make()
    tap.feed(sse(token(), final(timings={**TIMINGS, "prompt_n": 0, "prompt_per_second": 0.0})))
    s = finish(tap)
    assert s["prefill"] == {"tokens": 0, "tokens_per_second": None, "cached_tokens": 12}


def test_oversized_line_disables_tap_without_raising():
    tap, _ = make()
    tap.feed(b"data: " + b"x" * (1024 * 1024 + 10))
    tap.feed(sse(token(), final(timings=TIMINGS)))
    assert finish(tap) is None
    assert tap.live() is None


def test_garbage_never_raises():
    tap, _ = make()
    for chunk in (b"\xff\xfe\x00", b"data: {bad json}\n\n", b"data: [1,2]\n\n", b"data: 5\n\n"):
        tap.feed(chunk)
    tap.feed(sse({"choices": "nope"}, {"choices": [None, 3]}, token()))
    assert finish(tap)["generation"]["tokens"] == 1


def test_metrics_keeps_last_and_exposes_live():
    metrics = Metrics()
    assert metrics.view(None) == {"live": None, "last": None}
    tap, _ = make()
    tap.feed(sse(token(), final(timings=TIMINGS)))
    summary = finish(tap)
    metrics.record(summary)
    metrics.record(None)
    view = metrics.view(tap)
    assert view["last"] == summary
    assert view["live"]["tokens"] == 1


def test_real_llama_cpp_final_chunk_is_read_exactly():
    # Captured from llama-server b10902 on this host with a 48-token streamed request.
    from pathlib import Path

    chunk = json.loads(
        Path(__file__).with_name("fixtures").joinpath("llama-cpp-final-chunk.json").read_text()
    )
    tap, _ = make()
    tap.feed(sse(token(), token(), chunk))
    s = finish(tap)
    assert s["source"] == "engine"
    assert s["generation"] == {"tokens": 48, "tokens_per_second": 73.9}
    assert s["prefill"] == {"tokens": 19, "tokens_per_second": 140.8, "cached_tokens": 0}
    assert s["draft"] == {"proposed": 35, "accepted": 29, "acceptance": 0.829}
