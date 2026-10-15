"""Fourth-engine catalog and lifecycle integration; fixtures are not model inference."""

import json
import signal
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from gateway.app import create_app
from gateway.config import validate
from gateway.core import Manager, identity, signal_identity

ROOT = Path(__file__).resolve().parents[1]


def example_config():
    return validate(json.loads((ROOT / "examples/config.example.json").read_text()))


@pytest.mark.asyncio
async def test_four_engine_catalog_includes_kyojin_without_private_launch_details(tmp_path):
    cfg = example_config()
    cfg["allowed_hosts"] = ["testserver"]
    app = create_app(cfg, "a" * 32, "b" * 32, tmp_path)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": "Bearer " + "a" * 32},
        ) as client:
            catalog = (await client.get("/api/engines")).json()["engines"]
            assert {e["id"] for e in catalog} == {"llama", "strata", "kyojin", "dwarfstar"}
            kyojin = next(e for e in catalog if e["id"] == "kyojin")
            assert kyojin["label"] == "Kyojin"
            assert kyojin["models"] == [
                {"id": "qwen3.8-flash-next-kyojin", "context": 32768, "enabled": True}
            ]
            assert "/opt/" not in json.dumps(catalog)
            assert "argv" not in json.dumps(catalog)
            models = (await client.get("/v1/models")).json()["data"]
            assert next(m for m in models if m["owned_by"] == "kyojin")["status"] == "unloaded"
            selected = await client.post("/api/engine", json={"engine": "kyojin"})
            assert selected.status_code == 200
            assert (await client.get("/api/status")).json()["selected_engine"] == "kyojin"
    finally:
        await app.state.manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("family", ["qwen", "glm", "mimo"])
async def test_kyojin_native_matchers_detect_unmanaged_servers(tmp_path, family):
    cfg = example_config()
    script = f"/opt/kyojin/tools/{family}/serve.py"
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", script],
        start_new_session=True,
    )
    manager = Manager(cfg, tmp_path)
    try:
        assert identity(process.pid) in manager.native_processes()
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as error:
            await manager.select_engine("kyojin")
        assert error.value.status_code == 409
        assert not manager.owned
    finally:
        item = identity(process.pid)
        if item:
            signal_identity(item, signal.SIGTERM)
        process.wait(timeout=3)
        await manager.close()


@pytest.mark.asyncio
async def test_kyojin_configured_lifecycle_streams_and_verifies_exit(tmp_path):
    import socket

    cfg = example_config()
    spec = cfg["engines"]["kyojin"]["models"]["qwen3.8-flash-next-kyojin"]
    assert spec["probe"] == "slots"
    assert spec["ready_path"] == "/health"
    assert spec["argv"][spec["argv"].index("--model-id") + 1] == "qwen3.8-flash-next-kyojin"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    # Substitute only the unavailable external engine, retaining its probe/API config.
    fixture = str(Path(__file__).with_name("fixture_backend.py"))
    spec.update(
        argv=[sys.executable, fixture, str(port), str(tmp_path)],
        cwd=str(tmp_path),
        base_url=f"http://127.0.0.1:{port}",
    )
    cfg.update(allowed_hosts=["testserver"], startup_timeout=5)
    cfg["native_matchers"].append({"argv_contains": str(tmp_path)})
    app = create_app(cfg, "a" * 32, "b" * 32, tmp_path)
    process = None
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": "Bearer " + "a" * 32},
        ) as client:
            assert (await client.post("/api/engine", json={"engine": "kyojin"})).status_code == 200
            loaded = await client.post("/api/model", json={"model": "qwen3.8-flash-next-kyojin"})
            assert loaded.status_code == 200
            process = app.state.manager.process
            state = (await client.get("/api/status")).json()
            assert state["phase"] == "ready" and state["native_busy"] is False
            assert state["occupancy_source"] == "native_slots_and_gateway"
            response = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer " + "b" * 32},
                json={"model": "qwen3.8-flash-next-kyojin", "messages": [], "stream": True},
            )
            assert response.status_code == 200
            assert response.text == "data: fixture\n\ndata: [DONE]\n\n"
            assert (await client.post("/api/engine", json={"engine": "llama"})).status_code == 200
            assert process.poll() is not None
            assert (await client.get("/api/status")).json()["selected_engine"] == "llama"
            assert not app.state.manager.native_processes()
    finally:
        await app.state.manager.close()
        assert process is None or process.poll() is not None
