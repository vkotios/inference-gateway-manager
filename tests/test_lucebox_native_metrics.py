"""Native counters provide phase visibility without changing the response stream."""

import json

import httpx
import pytest

from gateway.lucebox_metrics import LuceboxMetrics
from gateway.metrics import MAX_BODY, Tap
from tests.test_metrics import Clock


def status(phase="decode", tokens=100, elapsed=10, **changes):
    return {
        "phase": phase,
        "active_requests": 0,
        "current": {
            "model": "m",
            "prompt_tokens": 2000,
            "completion_tokens": tokens,
            "elapsed_s": elapsed,
            "messages": "SECRET-PROMPT",
            **changes,
        },
    }


def setup():
    clock = Clock()
    tap = Tap(clock=clock)
    clock.now += 10
    return LuceboxMetrics(clock=clock), tap, clock


def test_decode_counters_work_while_response_is_buffered():
    native, tap, clock = setup()
    prefill = native.parse(status("prefill", 0), tap, ["m"])
    assert prefill["state"] == "prefill" and prefill["tokens"] == 0
    clock.now += 1
    first = native.parse(status(tokens=20, elapsed=11), tap, ["m"])
    assert first["state"] == "generating" and first["tokens_per_second"] is None
    clock.now += 1
    live = native.parse(status(tokens=40, elapsed=12), tap, ["m"])
    assert live["tokens_per_second"] == 20.0 and live["source"] == "engine"
    assert tap.events == 0
    assert "SECRET-PROMPT" not in json.dumps(live)


def test_internal_pass_or_counter_reset_cannot_create_a_rate_spike():
    native, tap, clock = setup()
    native.parse(status(tokens=3919, elapsed=10), tap, ["m"])
    clock.now += 1
    live = native.parse(status(tokens=2, elapsed=0.1), tap, ["m"])
    assert live["tokens_per_second"] is None
    clock.now += 1
    live = native.parse(status(tokens=20, elapsed=1.1), tap, ["m"])
    assert live["tokens_per_second"] == 18.0


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [],
        {"phase": "idle", "current": None},
        {**status(), "phase": "mixed"},
        {**status(), "active_requests": 2},
        status(model="other"),
        status(elapsed=9999),
        status(tokens=True),
        status(tokens=-1),
        status(tokens=10**400),
        status(elapsed=float("nan")),
        status(prompt_tokens=None),
    ],
)
def test_unattributable_or_invalid_snapshots_are_ignored(payload):
    native, tap, _ = setup()
    assert native.parse(payload, tap, ["m"]) is None


@pytest.mark.asyncio
async def test_poll_is_throttled_bounded_and_resets_for_a_new_request():
    native, tap, clock = setup()
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET" and request.url.path == "/status/json"
        assert request.extensions["timeout"]["read"] == 0.5
        return httpx.Response(200, json=status())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        live = await native.live(client, "http://127.0.0.1:8001", tap, ["m"])
        assert live["state"] == "generating"
        assert "SECRET-PROMPT" not in repr(vars(native))
        await native.live(client, "http://127.0.0.1:8001", tap, ["m"])
        assert len(calls) == 1
        clock.now += 1
        await native.live(client, "http://127.0.0.1:8001", tap, ["m"])
        assert len(calls) == 2
        next_tap = Tap(clock=clock)
        # New requests cannot consume a previous request's cached live view.
        assert await native.live(client, "http://127.0.0.1:8001", next_tap, ["m"]) is None
        assert len(calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "redirect", "oversized", "json"])
async def test_native_metrics_failures_remain_unknown(failure):
    native, tap, _ = setup()

    def handler(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("fixture", request=request)
        if failure == "redirect":
            return httpx.Response(302, headers={"Location": "http://external.invalid"})
        if failure == "oversized":
            return httpx.Response(200, content=b" " * (MAX_BODY + 1))
        return httpx.Response(200, content=b"not json")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await native.live(client, "http://127.0.0.1:8001", tap, ["m"]) is None
    assert tap.live()["state"] == "waiting"


@pytest.mark.asyncio
async def test_poll_cache_does_not_retain_a_completed_response_buffer():
    import gc
    import weakref

    native, tap, _ = setup()
    tap.feed(b'{"content":"SECRET-RESPONSE"}')
    reference = weakref.ref(tap)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=status()))
    async with httpx.AsyncClient(transport=transport) as client:
        await native.live(client, "http://127.0.0.1:8001", tap, ["m"])
    del tap
    gc.collect()
    assert reference() is None
