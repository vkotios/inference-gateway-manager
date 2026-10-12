import importlib.util

import httpx
import pytest


@pytest.mark.asyncio
async def test_admin_can_select_engine_but_inference_key_cannot(tmp_path):
    assert importlib.util.find_spec("gateway.app") is not None, "Authenticated API not implemented"
    from gateway.app import create_app

    config = {
        "engines": {"strata": {"label": "Strata", "models": {}}},
        "allowed_hosts": ["testserver"],
    }
    app = create_app(
        config,
        "admin-test-key-with-32-characters!!",
        "inference-test-key-with-32-chars!!",
        tmp_path,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        assert (await c.post("/api/engine", json={"engine": "strata"})).status_code == 401
        c.headers["Authorization"] = "Bearer inference-test-key-with-32-chars!!"
        assert (await c.post("/api/engine", json={"engine": "strata"})).status_code == 403
        c.headers["Authorization"] = "Bearer admin-test-key-with-32-characters!!"
        assert (await c.post("/api/engine", json={"engine": "strata"})).status_code == 200
        r = await c.get("/api/status")
        assert r.status_code == 200
        assert r.json()["selected_engine"] == "strata"
        assert r.json()["loaded_model"] is None
    await app.state.manager.close()
