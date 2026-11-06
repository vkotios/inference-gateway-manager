"""Strix Halo engine catalog, configuration and conflict tests; fixtures are not model inference."""

import json
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from gateway.app import create_app
from gateway.config import validate
from gateway.core import Manager, identity, signal_identity

ROOT = Path(__file__).resolve().parents[1]
STRIX_ENGINES = {"halogen", "halogen-flash", "talus", "strixite"}
EXPECTED_PROBES = {
    "halogen": "slots",
    "halogen-flash": "slots",
    "talus": "slots",
    "strixite": "gateway_only",
}


def example_config():
    return validate(json.loads((ROOT / "examples/config.example.json").read_text()))


def test_example_config_loads_all_strix_halo_engines():
    cfg = example_config()
    assert STRIX_ENGINES <= set(cfg["engines"])
    for engine in STRIX_ENGINES:
        spec = next(iter(cfg["engines"][engine]["models"].values()))
        assert spec["probe"] == EXPECTED_PROBES[engine]
        assert spec["base_url"].startswith("http://127.0.0.1:80")
        assert spec["context_limits"]["adjustable"] is True
        profile = next(iter(cfg["engines"][engine]["profiles"].values()))
        assert "{path}" in profile["argv"] or "{path}" in json.dumps(
            profile.get("config_template", {})
        )
    halogen = cfg["engines"]["halogen"]["profiles"]["hgn"]
    assert halogen["format"] == "native" and halogen["extensions"] == [".hgn"]
    for engine in ("halogen", "halogen-flash"):
        argv = cfg["engines"][engine]["models"][next(iter(cfg["engines"][engine]["models"]))][
            "argv"
        ]
        assert argv[0] in ("podman", "docker")
        assert "IGM_ENGINE_RUN_ID={launch_id}" in argv
        assert "/dev/kfd" in argv and "/dev/dri" in argv


@pytest.mark.asyncio
async def test_example_catalog_includes_strix_engines_without_private_launch_details(tmp_path):
    cfg = example_config()
    cfg["allowed_hosts"] = ["testserver"]
    cfg["native_matchers"] = [{"argv_contains": str(tmp_path)}]
    app = create_app(cfg, "a" * 32, "b" * 32, tmp_path)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": "Bearer " + "a" * 32},
        ) as client:
            catalog = (await client.get("/api/engines")).json()["engines"]
            assert STRIX_ENGINES <= {e["id"] for e in catalog}
            assert "/opt/" not in json.dumps(catalog)
            assert "argv" not in json.dumps(catalog)
            assert "ghcr.io" not in json.dumps(catalog)
            for engine_id in STRIX_ENGINES:
                engine = next(e for e in catalog if e["id"] == engine_id)
                model = engine["models"][0]
                assert model["enabled"] is True and model["context"] == 32768
                selected = await client.post("/api/engine", json={"engine": engine_id})
                assert selected.status_code == 200
                assert (await client.get("/api/status")).json()["selected_engine"] == engine_id
    finally:
        await app.state.manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "engine,executable",
    [
        ("halogen", "halogen-server"),
        ("halogen-flash", "halogen-flash-server"),
        ("talus", "talus-server"),
        ("strixite", "strixite"),
    ],
)
async def test_strix_native_matchers_detect_unmanaged_processes(
    tmp_path, monkeypatch, engine, executable
):
    config = example_config()
    process = SimpleNamespace(
        pid=987654,
        info={
            "exe": f"/opt/{engine}/bin/{executable}",
            "cmdline": [f"/opt/{engine}/bin/{executable}", "--model", "/models/model.gguf"],
        },
    )
    monkeypatch.setattr("gateway.core.psutil.process_iter", lambda attrs: [process])
    monkeypatch.setattr("gateway.core.identity", lambda pid: (pid, 123))
    manager = Manager(config, tmp_path)
    try:
        assert (987654, 123) in manager.native_processes()
        with pytest.raises(HTTPException) as exc:
            await manager.select_engine(engine)
        assert exc.value.status_code == 409
        assert manager.engine is None
        assert not manager.owned
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_talus_configured_lifecycle_streams_and_verifies_exit(tmp_path):
    import socket

    cfg = example_config()
    spec = cfg["engines"]["talus"]["models"]["qwen3.8-flash-next-talus"]
    assert spec["probe"] == "slots" and spec["ready_path"] == "/health"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    # Substitute only the unavailable external engine, retaining its probe/API config.
    fixture = str(Path(__file__).with_name("fixture_backend.py"))
    spec.update(
        argv=[sys.executable, fixture, str(port), str(tmp_path), "{context}"],
        cwd=str(tmp_path),
        base_url=f"http://127.0.0.1:{port}",
    )
    cfg.update(allowed_hosts=["testserver"], startup_timeout=5)
    # Scope this lifecycle fixture independently of engines running on the host.
    cfg["native_matchers"] = [{"argv_contains": str(tmp_path)}]
    app = create_app(cfg, "a" * 32, "b" * 32, tmp_path)
    process = None
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": "Bearer " + "a" * 32},
        ) as client:
            assert (await client.post("/api/engine", json={"engine": "talus"})).status_code == 200
            loaded = await client.post("/api/model", json={"model": "qwen3.8-flash-next-talus"})
            assert loaded.status_code == 200
            process = app.state.manager.process
            state = (await client.get("/api/status")).json()
            assert state["phase"] == "ready" and state["native_busy"] is False
            assert state["occupancy_source"] == "native_slots_and_gateway"
            response = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer " + "b" * 32},
                json={"model": "qwen3.8-flash-next-talus", "messages": [], "stream": True},
            )
            assert response.status_code == 200
            assert response.text == "data: fixture\n\ndata: [DONE]\n\n"
            assert (
                await client.post("/api/engine", json={"engine": "strixite"})
            ).status_code == 200
            assert process.poll() is not None
            assert (await client.get("/api/status")).json()["selected_engine"] == "strixite"
            assert not app.state.manager.native_processes()
    finally:
        await app.state.manager.close()
        assert process is None or process.poll() is not None


@pytest.mark.asyncio
async def test_strixite_native_matcher_detects_real_unmanaged_process(tmp_path):
    cfg = example_config()
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", "/opt/strixite/bin/strixite"],
        start_new_session=True,
        executable=sys.executable,
    )
    # The interpreter cannot rename its own exe; match on argv identity instead.
    cfg["native_matchers"] = [{"argv_contains": "/opt/strixite/bin/strixite"}]
    manager = Manager(cfg, tmp_path)
    try:
        assert identity(process.pid) in manager.native_processes()
        with pytest.raises(HTTPException) as error:
            await manager.select_engine("strixite")
        assert error.value.status_code == 409
        assert not manager.owned
    finally:
        item = identity(process.pid)
        if item:
            signal_identity(item, signal.SIGTERM)
        process.wait(timeout=3)
        await manager.close()
