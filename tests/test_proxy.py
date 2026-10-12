import socket
import sys
from pathlib import Path

import httpx
import pytest

from gateway.app import create_app


@pytest.mark.asyncio
async def test_stream_proxy_rejects_unselected_models_and_forwards_raw_sse(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    spec = {
        "argv": [sys.executable, str(Path(__file__).with_name("fixture_backend.py")), str(port)],
        "base_url": f"http://127.0.0.1:{port}",
        "upstream_model": "fixture",
    }
    cfg = {"allowed_hosts": ["testserver"], "engines": {"one": {"models": {"fixture": spec}}}}
    app = create_app(cfg, "a" * 32, "b" * 32, tmp_path)
    m = app.state.manager
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": "Bearer " + "a" * 32},
        ) as c:
            payload = {
                "model": "fixture",
                "messages": [{"role": "user", "content": "fixture"}],
                "stream": True,
            }
            assert (await c.post("/v1/chat/completions", json=payload)).status_code == 409
            await m.select_engine("one")
            await m.load_model("fixture")
            r = await c.post("/v1/chat/completions", json=payload)
            assert r.status_code == 200
            assert r.content == b"data: fixture\n\ndata: [DONE]\n\n"
            assert not m.active
            payload["model"] = "other"
            assert (await c.post("/v1/chat/completions", json=payload)).status_code == 409
    finally:
        await m.close()
