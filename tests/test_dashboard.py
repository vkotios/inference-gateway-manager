import httpx
import pytest

from gateway.app import create_app


@pytest.mark.asyncio
async def test_dashboard_catalog_and_real_host_telemetry(tmp_path):
    cfg = {
        "allowed_hosts": ["testserver"],
        "engines": {
            "strata": {
                "label": "Strata",
                "models": {
                    "qwen": {
                        "context": 262144,
                        "argv": ["PRIVATE_PATH"],
                        "base_url": "http://127.0.0.1:8081",
                    }
                },
            }
        },
    }
    app = create_app(cfg, "a" * 32, "b" * 32, tmp_path)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": "Bearer " + "a" * 32},
        ) as c:
            page = await c.get("/")
            assert page.status_code == 200
            assert "Inference Gateway" in page.text
            assert (await c.get("/assets/app.js")).status_code == 200
            catalog = await c.get("/api/engines")
            assert catalog.json()["engines"][0]["models"][0]["context"] == 262144
            assert "PRIVATE_PATH" not in catalog.text
            stats = (await c.get("/api/telemetry")).json()
            assert stats["memory"]["total_bytes"] > 0
            assert 0 <= stats["memory"]["pressure_bytes"] <= stats["memory"]["total_bytes"]
            assert isinstance(stats["gpus"], list)
            assert (await c.post("/api/unload", json={"force": False})).status_code == 200
            assert (await c.post("/api/model", json={"model": "qwen"})).status_code == 409
    finally:
        await app.state.manager.close()
