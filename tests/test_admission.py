import httpx
import pytest

from gateway.app import create_app


@pytest.mark.asyncio
async def test_unknown_slots_fail_closed_before_inference(tmp_path):
    cfg = {
        "allowed_hosts": ["testserver"],
        "engines": {
            "one": {"models": {"fixture": {"base_url": "http://127.0.0.1:1", "probe": "slots"}}}
        },
    }
    app = create_app(cfg, "a" * 32, "b" * 32, tmp_path)
    m = app.state.manager

    class Alive:
        def poll(self):
            return None

    m.engine, m.model, m.phase, m.process = "one", "fixture", "ready", Alive()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": "Bearer " + "b" * 32},
        ) as c:
            r = await c.post("/v1/chat/completions", json={"model": "fixture", "messages": []})
            assert r.status_code == 503
            assert not m.active
    finally:
        m.process = None
        m.model = None
        await m.close()
