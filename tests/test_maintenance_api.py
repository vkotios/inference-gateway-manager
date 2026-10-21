import json
import socket
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

from gateway.app import create_app
from gateway.core import Manager

ADMIN = "admin-test-key-with-32-characters!!"
INFERENCE = "inference-test-key-with-32-chars!!"
LEASE_ID = "0b1c2d3e-4f50-4a6b-8c7d-9e0f1a2b3c4d"


def build_config(tmp_path, maintenance=True, **extra):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    fixture = str(Path(__file__).with_name("fixture_backend.py").resolve())
    spec = {
        "argv": [sys.executable, fixture, str(port), str(tmp_path)],
        "base_url": f"http://127.0.0.1:{port}",
        "upstream_model": "fixture",
        "probe": "slots",
    }
    config = {
        "engines": {"one": {"models": {"fixture": spec}}, "two": {"models": {}}},
        "native_matchers": [{"argv_contains": str(tmp_path)}],
        "startup_timeout": 5,
        "allowed_hosts": ["testserver"],
        **extra,
    }
    if maintenance:
        config["maintenance"] = {
            "dir": str(tmp_path / "maint"),
            "holders": ["procurementmind-ci"],
            "max_ttl_seconds": 5400,
        }
    return config


def write_lease(tmp_path, **overrides):
    import time

    doc = {
        "version": 1,
        "id": LEASE_ID,
        "holder": "procurementmind-ci",
        "reason": "CI run 123",
        "acquired_at": time.time() - 1,
        "expires_at": time.time() + 600,
        **overrides,
    }
    path = tmp_path / "maint" / "lease.json"
    path.unlink(missing_ok=True)
    path.write_text(json.dumps(doc))
    path.chmod(0o600)
    return doc


@pytest.fixture
async def client(tmp_path):
    app = create_app(build_config(tmp_path), ADMIN, INFERENCE, tmp_path / "state")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {ADMIN}"},
    ) as c:
        c.app = app
        yield c
    await app.state.manager.close()


async def test_disabled_feature_changes_nothing(tmp_path):
    app = create_app(build_config(tmp_path, maintenance=False), ADMIN, INFERENCE, tmp_path / "s")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {ADMIN}"},
    ) as c:
        assert (await c.get("/api/maintenance")).json() == {"enabled": False}
        assert (await c.get("/api/status")).json()["maintenance"] == {"enabled": False}
        assert (await c.delete("/api/maintenance")).status_code == 404
        assert (await c.post("/api/engine", json={"engine": "one"})).status_code == 200
    await app.state.manager.close()


async def test_held_lease_blocks_model_and_engine_with_423(client, tmp_path):
    assert (await client.post("/api/engine", json={"engine": "one"})).status_code == 200
    write_lease(tmp_path)
    r = await client.post("/api/model", json={"model": "fixture"})
    assert r.status_code == 423
    assert "paused by 'procurementmind-ci'" in r.json()["detail"] and "CI run 123" in r.text
    assert 590 <= int(r.headers["retry-after"]) <= 601
    r = await client.post("/api/engine", json={"engine": "two"})
    assert r.status_code == 423 and "retry-after" in r.headers
    r = await client.post("/api/model", json={"model": "fixture", "force": True})
    assert r.status_code == 423
    status = (await client.get("/api/status")).json()
    assert status["phase"] == "selected" and status["selected_engine"] == "one"
    assert status["maintenance"]["lease"]["held"] is True


async def test_loaded_model_keeps_serving_and_unload_stays_allowed(client, tmp_path):
    await client.post("/api/engine", json={"engine": "one"})
    assert (await client.post("/api/model", json={"model": "fixture"})).status_code == 200
    write_lease(tmp_path)
    r = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {INFERENCE}"},
        json={"model": "fixture", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200
    assert (await client.post("/api/unload", json={})).status_code == 200


async def test_lease_expiry_and_invalid_lease_fail_open(client, tmp_path):
    await client.post("/api/engine", json={"engine": "one"})
    write_lease(tmp_path, holder="intruder")
    maint = client.app.state.maintenance
    assert (await client.get("/api/maintenance")).json()["lease"]["invalid"] is True
    assert (await client.post("/api/model", json={"model": "fixture"})).status_code == 200
    await client.post("/api/unload", json={})
    doc = write_lease(tmp_path)
    maint.clock = lambda: doc["expires_at"] + 1
    assert (await client.post("/api/model", json={"model": "fixture"})).status_code == 200


async def test_operator_breaks_lease_and_revocation_sticks(client, tmp_path):
    await client.post("/api/engine", json={"engine": "one"})
    write_lease(tmp_path)
    assert (await client.post("/api/model", json={"model": "fixture"})).status_code == 423
    r = await client.delete("/api/maintenance")
    assert r.status_code == 200
    assert r.json()["lease"]["id"] == LEASE_ID and r.json()["revoked_ids"] == [LEASE_ID]
    assert (await client.delete("/api/maintenance")).status_code == 404
    write_lease(tmp_path)  # the watcher renews the same id
    got = (await client.get("/api/maintenance")).json()
    assert got["lease"]["held"] is False and got["revoked_ids"] == [LEASE_ID]
    assert (await client.post("/api/model", json={"model": "fixture"})).status_code == 200
    await client.post("/api/unload", json={})
    write_lease(tmp_path, id="aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb")
    assert (await client.post("/api/model", json={"model": "fixture"})).status_code == 423


async def test_inference_key_cannot_read_or_break_the_lease(client, tmp_path):
    write_lease(tmp_path)
    weak = {"Authorization": f"Bearer {INFERENCE}"}
    assert (await client.get("/api/maintenance", headers=weak)).status_code == 403
    assert (await client.delete("/api/maintenance", headers=weak)).status_code == 403
    assert (await client.get("/api/maintenance", headers={"Authorization": ""})).status_code in (
        401,
        403,
    )


async def test_status_file_tracks_state_changes(client, tmp_path):
    status_file = tmp_path / "maint" / "status.json"
    await client.app.state.refresh_maintenance()
    first = json.loads(status_file.read_text())
    assert first["version"] == 1 and first["phase"] == "unloaded"
    assert first["lease"]["held"] is False and first["revoked_ids"] == []
    await client.post("/api/engine", json={"engine": "one"})
    await client.post("/api/model", json={"model": "fixture"})
    loaded = json.loads(status_file.read_text())
    assert loaded["phase"] == "ready" and loaded["loaded_model"] == "fixture"
    write_lease(tmp_path)
    await client.delete("/api/maintenance")
    broken = json.loads(status_file.read_text())
    assert broken["revoked_ids"] == [LEASE_ID] and broken["lease"]["held"] is False
    await client.post("/api/unload", json={})
    assert json.loads(status_file.read_text())["phase"] == "selected"


async def test_manager_rechecks_the_lease_inside_the_lifecycle_lock(tmp_path):
    manager = Manager(build_config(tmp_path, maintenance=False), tmp_path / "state")
    await manager.select_engine("one")
    manager.lease_guard = lambda: ("Model loads are paused by 'ci' until x (y)", 30)
    try:
        with pytest.raises(HTTPException) as caught:
            await manager.load_model("fixture")
        assert caught.value.status_code == 423 and caught.value.headers["Retry-After"] == "30"
        assert manager.process is None and manager.phase == "selected"
        with pytest.raises(HTTPException) as caught:
            await manager.select_engine("two")
        assert caught.value.status_code == 423 and manager.engine == "one"
        manager.lease_guard = lambda: None
        await manager.load_model("fixture")
        assert manager.phase == "ready"
    finally:
        await manager.close()


async def test_startup_default_load_is_not_gated_by_a_held_lease(tmp_path):
    config = build_config(tmp_path, default={"engine": "one", "model": "fixture"})
    (tmp_path / "maint").mkdir(mode=0o700)
    write_lease(tmp_path)
    app = create_app(config, ADMIN, INFERENCE, tmp_path / "state")
    async with app.router.lifespan_context(app):
        for _ in range(100):
            if app.state.manager.phase == "ready":
                break
            await __import__("asyncio").sleep(0.1)
        assert app.state.manager.phase == "ready"
        # Once startup is done the gate applies to later changes.
        assert app.state.manager.lease_guard is not None
