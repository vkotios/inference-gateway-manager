"""Lucebox responses retain generation timings even when tools buffer delivery."""

import json

from gateway.metrics import Tap
from tests.test_metrics import Clock, finish, make, sse, token

USAGE = {
    "prompt_tokens": 19013,
    "completion_tokens": 3919,
    "prompt_tokens_details": {"cached_tokens": 17932},
    "spec_decode_ran": False,
    "accept_rate": 0.0,
    "timings": {
        "prefill_ms": 6800.0,
        "prefilled_tokens": 1081,
        "cached_prefix_tokens": 17932,
        "decode_ms": 203000.0,
        "decode_tokens_per_sec": 19.3,
    },
}


def test_nested_usage_timings_are_read_from_plain_json():
    tap = Tap("application/json", clock=Clock())
    tap.feed(json.dumps({"choices": [], "usage": USAGE}).encode())
    result = finish(tap)
    assert result["source"] == "engine"
    assert result["generation"] == {"tokens": 3919, "tokens_per_second": 19.3}
    assert result["prefill"] == {"tokens": 1081, "tokens_per_second": 159.0, "cached_tokens": 17932}
    assert result["draft"] is None


def test_buffered_sse_uses_native_rate_instead_of_delivery_burst():
    tap, clock = make()
    clock.now += 209.8
    tap.feed(sse(token("answer")))
    clock.now += 0.00001
    tap.feed(sse(token("rest")))
    tap.feed(sse({"choices": [], "usage": USAGE}))
    result = finish(tap)
    assert result["source"] == "engine"
    assert result["generation"]["tokens_per_second"] == 19.3
    assert result["time_to_first_token_seconds"] == 209.8


def test_burst_without_native_timings_has_no_invented_rate():
    tap, clock = make()
    for _ in range(4):
        tap.feed(sse(token()))
        clock.now += 0.00001
    tap.feed(sse({"usage": {"completion_tokens": 3919}}))
    assert tap.live()["tokens_per_second"] is None
    assert finish(tap)["generation"]["tokens_per_second"] is None


def test_zero_prefilled_tokens_stay_zero_on_full_cache_hit():
    tap, _ = make()
    usage = {**USAGE, "timings": {**USAGE["timings"], "prefilled_tokens": 0}}
    tap.feed(sse({"usage": usage}))
    result = finish(tap)
    assert result["prefill"]["tokens"] == 0
    assert result["prefill"]["tokens_per_second"] is None


def test_speculative_acceptance_does_not_invent_missing_draft_counts():
    tap, _ = make()
    tap.feed(sse({"usage": {**USAGE, "spec_decode_ran": True, "accept_rate": 0.625}}))
    assert finish(tap)["draft"] == {"proposed": None, "accepted": None, "acceptance": 0.625}
