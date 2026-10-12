"""Inference response lifetime and strict JSON regression tests."""

import asyncio
import json
import socket
import sys
from pathlib import Path

import httpx
import pytest

from gateway.app import create_app


def app_for(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    spec = {
        "argv": [sys.executable, str(Path(__file__).with_name("fixture_backend.py")), str(port)],
        "base_url": f"http://127.0.0.1:{port}",
    }
    return create_app(
        {
            "allowed_hosts": ["testserver"],
            "engines": {"one": {"models": {"fixture": spec}}},
            "stop_timeout": 0.1,
        },
        "a" * 32,
        "b" * 32,
        tmp_path,
    )


@pytest.mark.asyncio
async def test_s3_force_unload_cancels_stalled_consumer_and_allows_reload(tmp_path):
    app = app_for(tmp_path)
    manager = app.state.manager
    await manager.select_engine("one")
    await manager.load_model("fixture")
    body = json.dumps({"model": "fixture", "stream": True}).encode()
    stalled, release = asyncio.Event(), asyncio.Event()
    delivered = False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.Event().wait()

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            stalled.set()
            await release.wait()  # Models server socket drain on a slow peer.

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"testserver"),
            (b"authorization", b"Bearer " + b"b" * 32),
            (b"content-type", b"application/json"),
        ],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
    }
    response = asyncio.create_task(app(scope, receive, send))
    try:
        await asyncio.wait_for(stalled.wait(), 3)
        assert manager.active
        await manager.unload(force=True)
        assert not manager.active, "Force stop retained the previous engine's admission"
        await asyncio.sleep(0.05)
        assert response.done(), "Force stop left the ASGI response stalled in send"
        await manager.load_model("fixture")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": "Bearer " + "b" * 32},
        ) as client:
            result = await client.post("/v1/chat/completions", json={"model": "fixture"})
            assert result.status_code == 200
        await manager.unload()
    finally:
        release.set()
        response.cancel()
        await asyncio.gather(response, return_exceptions=True)
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    [b"NaN", b"Infinity", b"-Infinity", b"1e999", b"[" * 2000 + b"0" + b"]" * 2000],
    ids=["nan", "infinity", "negative-infinity", "overflow", "nested"],
)
async def test_l8_invalid_json_is_400_and_never_holds_admission(tmp_path, invalid):
    app = app_for(tmp_path)
    manager = app.state.manager
    await manager.select_engine("one")
    await manager.load_model("fixture")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://testserver",
            headers={"Authorization": "Bearer " + "b" * 32},
        ) as client:
            result = await client.post(
                "/v1/chat/completions",
                content=b'{"model":"fixture","x":' + invalid + b"}",
                headers={"Content-Type": "application/json"},
            )
            assert result.status_code == 400
            assert not manager.active
    finally:
        await manager.close()
