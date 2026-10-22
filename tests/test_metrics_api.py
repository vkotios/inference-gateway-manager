"""Generation metrics through the real proxy: passive, additive and content-free."""

import asyncio
import json
import socket
import sys
from pathlib import Path

import httpx
import pytest

from gateway.app import create_app

TIMINGS = {
    "prompt_n": 64,
    "prompt_per_second": 150.0,
    "cache_n": 0,
    "predicted_n": 5,
    "predicted_per_second": 40.0,
}
EVENTS = [
    {"choices": [{"delta": {"role": "assistant", "content": ""}}]},
    *[{"choices": [{"delta": {"content": "SECRET-TEXT"}}]} for _ in range(5)],
    {"choices": [{"delta": {}, "finish_reason": "stop"}], "timings": TIMINGS},
]
REPLY = {"choices": [{"message": {"content": "hi"}}], "timings": TIMINGS}


def make_app(tmp_path, delay=0.0):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    script, reply = tmp_path / "sse.json", tmp_path / "reply.json"
    script.write_text(json.dumps(EVENTS))
    reply.write_text(json.dumps(REPLY))
    fixture = str(Path(__file__).with_name("fixture_generation_backend.py").resolve())
    spec = {
        "argv": [
            sys.executable,
            fixture,
            str(port),
            str(tmp_path),
            f"--sse-script={script}",
            f"--json-reply={reply}",
            f"--sse-delay={delay}",
        ],
        "base_url": f"http://127.0.0.1:{port}",
        "upstream_model": "fixture",
        "probe": "slots",
    }
    config = {
        "engines": {"one": {"models": {"fixture": spec}}},
        "native_matchers": [{"argv_contains": str(tmp_path)}],
        "startup_timeout": 5,
        "allowed_hosts": ["testserver"],
    }
    return create_app(config, "a" * 32, "b" * 32, tmp_path)


def client(app, key):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Authorization": "Bearer " + key * 32},
    )


async def load(app):
    await app.state.manager.select_engine("one")
    await app.state.manager.load_model("fixture")


def chat(**extra):
    return {"model": "fixture", "messages": [{"role": "user", "content": "hi"}], **extra}


@pytest.mark.asyncio
async def test_status_reports_nothing_before_any_request(tmp_path):
    app = make_app(tmp_path)
    try:
        async with client(app, "a") as admin:
            status = (await admin.get("/api/status")).json()
        assert status["generation"] == {"live": None, "last": None}
    finally:
        await app.state.manager.close()


@pytest.mark.asyncio
async def test_streamed_request_is_reported_and_bytes_are_untouched(tmp_path):
    app = make_app(tmp_path)
    try:
        await load(app)
        async with client(app, "b") as inference, client(app, "a") as admin:
            r = await inference.post(
                "/v1/chat/completions", json=chat(stream=True), headers={"x-client-name": "hermes"}
            )
            expected = b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in EVENTS)
            assert r.status_code == 200 and r.content == expected + b"data: [DONE]\n\n"
            last = (await admin.get("/api/status")).json()["generation"]["last"]
        assert last["source"] == "engine" and last["outcome"] == "complete"
        assert last["model"] == "fixture" and last["client"] == "hermes"
        assert last["generation"] == {"tokens": 5, "tokens_per_second": 40.0}
        assert last["prefill"]["tokens_per_second"] == 150.0 and last["prefill"]["tokens"] == 64
        assert last["time_to_first_token_seconds"] is not None
        assert "SECRET-TEXT" not in json.dumps(last)
    finally:
        await app.state.manager.close()


@pytest.mark.asyncio
async def test_non_streamed_request_uses_engine_timings(tmp_path):
    app = make_app(tmp_path)
    try:
        await load(app)
        async with client(app, "b") as inference, client(app, "a") as admin:
            r = await inference.post("/v1/chat/completions", json=chat())
            assert r.json() == REPLY
            last = (await admin.get("/api/status")).json()["generation"]["last"]
        assert last["source"] == "engine" and last["generation"]["tokens_per_second"] == 40.0
    finally:
        await app.state.manager.close()


@pytest.mark.asyncio
async def test_other_inference_endpoints_are_reported_too(tmp_path):
    app = make_app(tmp_path)
    try:
        await load(app)
        async with client(app, "b") as inference, client(app, "a") as admin:
            r = await inference.post("/v1/completions", json={"model": "fixture", "prompt": "x"})
            assert r.status_code == 200
            last = (await admin.get("/api/status")).json()["generation"]["last"]
        assert last["generation"]["tokens_per_second"] == 40.0
    finally:
        await app.state.manager.close()


@pytest.mark.asyncio
async def test_live_view_is_reported_while_a_request_runs(tmp_path):
    app = make_app(tmp_path, delay=0.3)
    try:
        await load(app)
        async with client(app, "b") as inference, client(app, "a") as admin:
            request = asyncio.create_task(
                inference.post("/v1/chat/completions", json=chat(stream=True))
            )
            live = None
            for _ in range(40):
                await asyncio.sleep(0.1)
                live = (await admin.get("/api/status")).json()["generation"]["live"]
                if live and live["tokens"] >= 4:
                    break
            assert live is not None and live["state"] == "generating"
            assert live["tokens"] >= 4 and 1 < live["tokens_per_second"] < 10
            await request
            done = (await admin.get("/api/status")).json()["generation"]
        assert done["live"] is None and done["last"]["outcome"] == "complete"
    finally:
        await app.state.manager.close()


@pytest.mark.asyncio
async def test_failed_request_does_not_overwrite_last(tmp_path):
    app = make_app(tmp_path)
    try:
        await load(app)
        async with client(app, "b") as inference, client(app, "a") as admin:
            await inference.post("/v1/chat/completions", json=chat())
            before = (await admin.get("/api/status")).json()["generation"]["last"]
            r = await inference.post("/v1/chat/completions", json=chat(model="other"))
            assert r.status_code == 409
            assert (await admin.get("/api/status")).json()["generation"]["last"] == before
    finally:
        await app.state.manager.close()
