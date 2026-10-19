"""Loading must hold until native slots report idle, so the first call is admitted."""

import asyncio

import httpx
import pytest
from test_review_lifecycle import config

from gateway.app import create_app


def warmup_config(tmp_path):
    cfg = config(tmp_path)
    spec = cfg["engines"]["one"]["models"]["fixture"]
    marker = tmp_path / "warmup-busy"
    marker.touch()
    spec["argv"] = spec["argv"] + [str(marker)]
    cfg["warmup_timeout"] = 2
    return cfg, marker


@pytest.mark.asyncio
async def test_load_holds_until_slots_idle_then_first_call_admitted(tmp_path):
    cfg, marker = warmup_config(tmp_path)
    app = create_app(cfg, "a" * 32, "b" * 32, tmp_path)
    manager = app.state.manager
    try:
        await manager.select_engine("one")
        load = asyncio.create_task(manager.load_model("fixture"))
        await asyncio.sleep(0.5)
        assert manager.phase == "loading", "health is up but warmup still occupies the slot"
        assert not load.done()
        marker.unlink()
        await asyncio.wait_for(load, 5)
        assert manager.phase == "ready"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": "Bearer " + "b" * 32},
        ) as client:
            r = await client.post("/v1/chat/completions", json={"model": "fixture", "messages": []})
            assert r.status_code == 200
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_warmup_timeout_still_reaches_ready(tmp_path):
    cfg, marker = warmup_config(tmp_path)
    cfg["warmup_timeout"] = 0.3
    app = create_app(cfg, "a" * 32, "b" * 32, tmp_path)
    manager = app.state.manager
    try:
        await manager.select_engine("one")
        await asyncio.wait_for(manager.load_model("fixture"), 10)
        assert manager.phase == "ready", "warmup timeout degrades to ready, not error"
    finally:
        marker.unlink(missing_ok=True)
        await manager.close()
