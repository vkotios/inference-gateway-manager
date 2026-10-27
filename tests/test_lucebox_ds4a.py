"""Portable engine contracts and disposable lifecycle tests; no GPU inference."""

import copy
import json
import socket
import struct
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from gateway.app import create_app
from gateway.config import validate
from gateway.core import Manager
from gateway.registry import Registry
from gateway.settings import select

ROOT = Path(__file__).resolve().parents[1]


def example_config():
    return validate(json.loads((ROOT / "examples/config.example.json").read_text()))


def test_existing_ds4_entry_is_preserved_and_new_examples_are_opt_in():
    cfg = example_config()
    ds4 = cfg["engines"]["dwarfstar"]
    assert ds4["models"]["deepseek-v4-flash"]["argv"][0] == "/opt/ds4/ds4-server"
    assert ds4["profiles"]["gguf"]["argv"][0] == "/opt/ds4/ds4-server"
    assert ds4["models"]["deepseek-v4-flash"].get("enabled", True)
    assert "default" not in cfg
    for engine in ("lucebox", "ds4a"):
        spec = cfg["engines"][engine]["models"][f"example-{engine}"]
        assert spec["enabled"] is False
        validate({"engines": {engine: {"models": {engine: dict(spec, enabled=True)}}}})
        assert spec["probe"] == "gateway_only"
        assert spec["base_url"].startswith("http://127.0.0.1:")
        assert all("/home/" not in a for a in spec["argv"])
        profile = next(iter(cfg["engines"][engine]["profiles"].values()))
        for context in (4096, 8192, 16384, 32768, 65536, 131072):
            selected, _ = select(profile, context)
            flag = "--max-ctx" if engine == "lucebox" else "--ctx"
            assert selected["argv"][selected["argv"].index(flag) + 1] == str(context)
        with pytest.raises(HTTPException):
            select(profile, 262144)
    luce = cfg["engines"]["lucebox"]["profiles"]["gguf"]
    assert "--draft" not in luce["argv"] and not luce.get("artifacts")
    assert luce["env"]["LUCE_DS4_SPEC"] == "0"
    ds4a = cfg["engines"]["ds4a"]["profiles"]["dspark-gguf"]
    assert ds4a["upstream_model"] == "deepseek-v4-flash"
    assert ds4a["argv"][ds4a["argv"].index("--mtp") + 1] == "{artifact.draft}"
    assert "--dspark" in ds4a["argv"] and "--rocm" in ds4a["argv"]


@pytest.mark.parametrize("engine,profile", [("lucebox", "gguf"), ("ds4a", "dspark-gguf")])
def test_profiles_register_confined_artifacts_without_loading(tmp_path, engine, profile):
    cfg = example_config()
    root = tmp_path / "weights"
    root.mkdir()

    def string(value):
        raw = value.encode()
        return struct.pack("<Q", len(raw)) + raw

    gguf = (
        b"GGUF"
        + struct.pack("<IQQ", 3, 0, 1)
        + string("general.architecture")
        + struct.pack("<I", 8)
        + string("deepseek4")
    )
    target = root / "target.gguf"
    target.write_bytes(gguf)
    draft = root / "draft.gguf"
    draft.write_bytes(gguf)
    cfg["model_roots"] = {"weights": str(root)}
    registry = Registry(cfg, tmp_path)
    candidate = {"engine": engine, "profile": profile, "path": str(target), "model": "test-model"}
    if engine == "ds4a":
        with pytest.raises(HTTPException):
            registry.inspect(candidate)
        candidate["artifacts"] = {"draft": str(tmp_path / "outside.gguf")}
        (tmp_path / "outside.gguf").write_bytes(gguf)
        with pytest.raises(HTTPException):
            registry.inspect(candidate)
        candidate["artifacts"] = {"draft": str(draft)}
    registry.register(candidate)
    spec = cfg["engines"][engine]["models"]["test-model"]
    assert str(target) in spec["argv"]
    assert "{path}" not in spec["argv"]
    if engine == "ds4a":
        assert str(draft) in spec["argv"]
    assert registry.path.exists()


@pytest.mark.parametrize("exe", ["luce_server", "ds4-server", "ds4a-server", "ds4fa-server"])
async def test_native_matchers_block_unmanaged_engines_without_claiming_ownership(
    tmp_path, monkeypatch, exe
):
    # Synthetic /proc inventory avoids interacting with the host's real engines.
    import gateway.core as core

    process = SimpleNamespace(pid=987654, info={"exe": f"/opt/engine/{exe}", "cmdline": [exe]})
    unrelated = SimpleNamespace(pid=987655, info={"exe": "/usr/bin/python", "cmdline": ["python"]})
    monkeypatch.setattr(core.psutil, "process_iter", lambda attrs: [process, unrelated])
    monkeypatch.setattr(core, "identity", lambda pid: (pid, 123))
    manager = Manager(example_config(), tmp_path)
    try:
        assert manager.native_processes() == [(process.pid, 123)]
        with pytest.raises(HTTPException) as exc:
            await manager.select_engine("lucebox")
        assert exc.value.status_code == 409
        assert not manager.owned and manager.process is None
    finally:
        await manager.close()


async def test_engine_selection_is_explicit_and_switch_verifies_fixture_exit(tmp_path):
    cfg = example_config()
    cfg.update(allowed_hosts=["testserver"], startup_timeout=5)
    cfg["native_matchers"] = [{"argv_contains": str(tmp_path)}]
    for engine in ("lucebox", "ds4a"):
        spec = cfg["engines"][engine]["models"][f"example-{engine}"]
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        spec.update(
            enabled=True,
            argv=[
                sys.executable,
                str(ROOT / "tests/fixture_backend.py"),
                str(port),
                str(tmp_path),
                "{context}",
            ],
            base_url=f"http://127.0.0.1:{port}",
        )
    cfg = validate(copy.deepcopy(cfg))
    app = create_app(cfg, "a" * 32, "b" * 32, tmp_path)
    manager = app.state.manager
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": "Bearer " + "a" * 32},
        ) as client:
            catalog = (await client.get("/api/engines")).json()["engines"]
            assert {e["id"] for e in catalog} >= {"dwarfstar", "lucebox", "ds4a"}
            assert "/opt/" not in json.dumps(catalog)
            for engine in ("lucebox", "ds4a"):
                model = f"example-{engine}"
                assert (
                    await client.post("/api/engine", json={"engine": engine})
                ).status_code == 200
                assert manager.process is None
                # Inference does not implicitly load the selected engine/model.
                assert (
                    await client.post("/v1/chat/completions", json={"model": model})
                ).status_code == 409
                assert manager.process is None
                loaded = await client.post("/api/model", json={"model": model, "context": 65536})
                assert loaded.status_code == 200
                state = loaded.json()
                assert state["occupancy_source"] == "gateway_only"
                assert state["active_settings"]["context_source"] != "native_slots"
                process = manager.process
                response = await client.post(
                    "/v1/chat/completions", json={"model": model, "messages": [], "stream": True}
                )
                assert response.content == b"data: fixture\n\ndata: [DONE]\n\n"
                assert (
                    await client.post("/api/engine", json={"engine": "dwarfstar"})
                ).status_code == 200
                assert process.poll() is not None and not manager.native_processes()
    finally:
        await manager.close()


async def test_disabled_examples_are_discoverable_but_cannot_launch(tmp_path):
    cfg = example_config()
    cfg.update(allowed_hosts=["testserver"], native_matchers=[{"argv_contains": str(tmp_path)}])
    app = create_app(cfg, "a" * 32, "b" * 32, tmp_path)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": "Bearer " + "a" * 32},
        ) as client:
            catalog = (await client.get("/api/engines")).json()["engines"]
            for engine in ("lucebox", "ds4a"):
                entry = next(e for e in catalog if e["id"] == engine)
                assert entry["models"][0]["enabled"] is False
                assert entry["profiles"]
                response = await client.post(
                    "/api/model", json={"engine": engine, "model": f"example-{engine}"}
                )
                assert response.status_code == 404
                assert app.state.manager.process is None
    finally:
        await app.state.manager.close()
