import asyncio
import copy
import json
import socket
import struct
import sys
from pathlib import Path

import httpx
import pytest

from gateway.app import create_app


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["artifact", "root", "profile", "engine", "collision"])
async def test_unavailable_registration_keeps_gateway_running_and_can_be_removed(
    tmp_path, failure, caplog
):
    from gateway.registry import Registry

    cfg = registry_config(tmp_path)
    static = {"argv": ["/bin/false"], "base_url": "http://127.0.0.1:8081", "context": 2048}
    cfg["engines"]["llama"]["models"]["static"] = static
    state = tmp_path / "state"
    Registry(copy.deepcopy(cfg), state).register(
        {
            "engine": "llama",
            "path": str(tmp_path / "models/model.gguf"),
            "model": "gone",
        }
    )
    if failure == "artifact":
        (tmp_path / "models/model.gguf").unlink()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        static.update(
            argv=[
                sys.executable,
                str(Path(__file__).with_name("fixture_backend.py")),
                str(port),
                str(tmp_path),
            ],
            base_url=f"http://127.0.0.1:{port}",
        )
    elif failure == "root":
        cfg["model_roots"] = {}
    elif failure == "profile":
        cfg["engines"]["llama"]["profiles"] = {}
    elif failure == "engine":
        cfg["engines"]["other"] = cfg["engines"].pop("llama")
    else:
        cfg["engines"]["llama"]["models"]["gone"] = copy.deepcopy(static)
    app = create_app(cfg, "a" * 32, "b" * 32, state)
    logs = [record.getMessage() for record in caplog.records if record.name == "gateway.registry"]
    assert any("llama/gone" in message and "unavailable" in message for message in logs)
    assert all(str(tmp_path) not in message for message in logs)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": "Bearer " + "a" * 32},
        ) as client:
            assert (await client.get("/health")).status_code == 200
            catalog = (await client.get("/api/engines")).json()
            unavailable = catalog["unavailable_registrations"]
            assert [(entry["engine"], entry["id"]) for entry in unavailable] == [("llama", "gone")]
            assert "unavailable" in unavailable[0]["reason"].lower()
            models = catalog["engines"][0]["models"]
            assert next(m for m in models if m["id"] == "static")["enabled"] is True
            if failure not in {"engine", "collision"}:
                assert next(m for m in models if m["id"] == "gone")["enabled"] is False
                assert (
                    await client.post("/api/model", json={"engine": "llama", "model": "gone"})
                ).status_code == 404
            assert (
                await client.delete(
                    "/api/models/llama/gone", headers={"Authorization": "Bearer " + "b" * 32}
                )
            ).status_code == 403
            removed = await client.delete("/api/models/llama/gone")
            assert removed.status_code == 200 and removed.json()["removed"] is True
            assert json.loads((state / "models.json").read_text()) == []
            assert (await client.get("/api/engines")).json()["unavailable_registrations"] == []
            if failure == "collision":
                assert cfg["engines"]["llama"]["models"]["gone"] == static
            assert (await client.delete("/api/models/llama/static")).status_code == 404
            if failure == "artifact":
                assert (
                    await client.post("/api/model", json={"engine": "llama", "model": "static"})
                ).status_code == 200
    finally:
        await app.state.manager.close()
    fresh = create_app(cfg, "a" * 32, "b" * 32, state)
    await fresh.state.manager.close()


def registry_config(tmp_path):
    root = tmp_path / "models"
    root.mkdir(exist_ok=True)
    (root / "model.gguf").write_bytes(b"GGUF" + struct.pack("<IQQ", 3, 0, 0))
    return {
        "allowed_hosts": ["testserver"],
        "model_roots": {"local": str(root)},
        "engines": {
            "llama": {
                "models": {},
                "profiles": {
                    "gguf": {
                        "format": "gguf",
                        "argv": [
                            "/bin/false",
                            "-m",
                            "{path}",
                            "--alias",
                            "{model}",
                            "--ctx-size",
                            "{context}",
                        ],
                        "base_url": "http://127.0.0.1:8080",
                        "context": 4096,
                        "context_limits": {
                            "min": 1024,
                            "max": 8192,
                            "presets": [4096, 8192],
                            "adjustable": True,
                        },
                    }
                },
            }
        },
    }


@pytest.mark.asyncio
async def test_removing_a_loaded_registration_requires_unload_and_keeps_artifacts(tmp_path):
    cfg = registry_config(tmp_path)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    cfg["engines"]["llama"]["profiles"]["gguf"].update(
        argv=[
            sys.executable,
            str(Path(__file__).with_name("fixture_backend.py")),
            str(port),
            str(tmp_path),
            "{context}",
        ],
        base_url=f"http://127.0.0.1:{port}",
    )
    cfg["startup_timeout"] = 5
    app = create_app(cfg, "a" * 32, "b" * 32, tmp_path / "state")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": "Bearer " + "a" * 32},
        ) as client:
            registered = await client.post(
                "/api/models",
                json={
                    "engine": "llama",
                    "model": "saved",
                    "path": str(tmp_path / "models/model.gguf"),
                },
            )
            assert registered.status_code == 201
            assert (
                await client.post("/api/model", json={"engine": "llama", "model": "saved"})
            ).status_code == 200
            process = app.state.manager.process
            assert (await client.delete("/api/models/llama/saved")).status_code == 409
            assert app.state.manager.process is process and process.poll() is None
            assert (await client.post("/api/unload", json={})).status_code == 200
            assert (await client.delete("/api/models/llama/saved")).status_code == 200
            assert (tmp_path / "models/model.gguf").is_file()
            assert json.loads((tmp_path / "state/models.json").read_text()) == []
            assert (await client.get("/api/engines")).json()["engines"][0]["models"] == []
    finally:
        await app.state.manager.close()


@pytest.mark.asyncio
async def test_registry_inspect_register_persist_and_duplicates(tmp_path):
    cfg = registry_config(tmp_path)
    app = create_app(cfg, "a" * 32, "b" * 32, tmp_path / "state")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": "Bearer " + "a" * 32},
        ) as c:
            catalog = (await c.get("/api/engines")).json()["engines"]
            assert catalog[0]["profiles"] == [
                {"id": "gguf", "label": "gguf", "format": "gguf", "artifacts": []}
            ]
            roots = await c.get("/api/model-files")
            assert roots.status_code == 200 and roots.json()["roots"] == [
                {"id": "local", "label": "local"}
            ]
            listing = (await c.get("/api/model-files?root=local")).json()
            assert listing["entries"] == [
                {"name": "model.gguf", "path": "model.gguf", "kind": "file"}
            ]
            result = await c.post(
                "/api/models/inspect",
                json={
                    "engine": "llama",
                    "path": str(tmp_path / "models/model.gguf"),
                    "model": "registered",
                },
            )
            assert result.status_code == 200
            candidate = result.json()["candidate"]
            assert "argv" not in candidate
            results = await asyncio.gather(
                *[c.post("/api/models", json=candidate) for _ in range(2)]
            )
            assert sorted(r.status_code for r in results) == [201, 409]
            assert app.state.manager.model is None
        stored = json.loads((tmp_path / "state/models.json").read_text())
        assert "argv" not in json.dumps(stored)
        fresh = create_app(registry_config(tmp_path), "a" * 32, "b" * 32, tmp_path / "state")
        assert "registered" in fresh.state.manager.config["engines"]["llama"]["models"]
        await fresh.state.manager.close()
    finally:
        await app.state.manager.close()


@pytest.mark.asyncio
async def test_registry_rejects_escape_sidecars_and_command_injection(tmp_path):
    cfg = registry_config(tmp_path)
    outside = tmp_path / "secret.gguf"
    outside.write_bytes(b"GGUF" + struct.pack("<IQQ", 3, 0, 0))
    (tmp_path / "models/escape.gguf").symlink_to(outside)
    app = create_app(cfg, "a" * 32, "b" * 32, tmp_path / "state")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": "Bearer " + "a" * 32},
        ) as c:
            for path in ("../", "/tmp"):
                assert (
                    await c.get("/api/model-files", params={"root": "local", "path": path})
                ).status_code == 422
            for path in (outside, tmp_path / "models/escape.gguf"):
                assert (
                    await c.post("/api/models/inspect", json={"engine": "llama", "path": str(path)})
                ).status_code == 422
            candidate = (
                await c.post(
                    "/api/models/inspect",
                    json={"engine": "llama", "path": str(tmp_path / "models/model.gguf")},
                )
            ).json()["candidate"]
            assert (
                await c.post("/api/models", json={**candidate, "argv": ["/bin/sh"]})
            ).status_code == 422
    finally:
        await app.state.manager.close()


@pytest.mark.asyncio
async def test_pack_and_named_companions_stay_confined(tmp_path):
    cfg = registry_config(tmp_path)
    pack = tmp_path / "models/pack"
    pack.mkdir()
    (pack / "config.json").write_text("{}")
    (pack / "tokenizer.json").write_text("{}")
    (pack / "weights.safetensors").write_bytes(b"safe")
    cfg["engines"]["llama"]["profiles"]["pack"] = {
        "format": "kyojin",
        "argv": ["/bin/false", "--pack", "{path}", "--tokenizer", "{artifact.tokenizer}"],
        "base_url": "http://127.0.0.1:8080",
        "context": 4096,
        "artifacts": {"tokenizer": {"kind": "directory", "required_files": ["tokenizer.json"]}},
    }
    app = create_app(cfg, "a" * 32, "b" * 32, tmp_path / "state")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": "Bearer " + "a" * 32},
        ) as c:
            body = {
                "engine": "llama",
                "profile": "pack",
                "path": str(pack),
                "artifacts": {"tokenizer": str(pack)},
            }
            assert (await c.post("/api/models/inspect", json=body)).status_code == 200
            (pack / "unsafe.bin").write_bytes(b"pickle")
            assert (await c.post("/api/models/inspect", json=body)).status_code == 422
            (pack / "unsafe.bin").unlink()
            (pack / "escape.json").symlink_to(tmp_path / "outside.json")
            (tmp_path / "outside.json").write_text("{}")
            assert (await c.post("/api/models/inspect", json=body)).status_code == 422
    finally:
        await app.state.manager.close()


@pytest.mark.asyncio
async def test_split_gguf_refuses_missing_shards_and_rechecks_registration(tmp_path):
    cfg = registry_config(tmp_path)
    root = tmp_path / "models"
    first = root / "split-00001-of-00002.gguf"
    first.write_bytes(b"GGUF" + struct.pack("<IQQ", 3, 0, 0))
    second = root / "split-00002-of-00002.gguf"
    app = create_app(cfg, "a" * 32, "b" * 32, tmp_path / "state")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": "Bearer " + "a" * 32},
        ) as c:
            body = {"engine": "llama", "path": str(first)}
            assert (await c.post("/api/models/inspect", json=body)).status_code == 422
            second.write_bytes(b"GGUF" + struct.pack("<IQQ", 3, 0, 0))
            candidate = (await c.post("/api/models/inspect", json=body)).json()["candidate"]
            second.unlink()
            assert (await c.post("/api/models", json=candidate)).status_code == 422
            assert not (tmp_path / "state/models.json").exists()
    finally:
        await app.state.manager.close()


@pytest.mark.asyncio
async def test_strata_native_pack_requires_real_shards_and_rejects_escaping_index(tmp_path):
    cfg = registry_config(tmp_path)
    root = tmp_path / "models"
    pack = root / "strata-pack"
    pack.mkdir()
    (pack / "tokenizer").mkdir()
    (pack / "tokenizer/tokenizer.json").write_text("{}")
    (pack / "dense.bin").write_bytes(b"native dense data")
    (pack / "index.txt").write_text("# native dense offsets\n")
    (pack / "native_experts.txt").write_text("0 12 7 0 1 2 3 4 model.gguf\n")
    cfg["engines"]["llama"]["profiles"]["strata"] = {
        "format": "strata",
        "argv": ["/bin/false", "--pack", "{path}", "--native", "{artifact.native}"],
        "base_url": "http://127.0.0.1:8080",
        "context": 4096,
        "required_files": [
            "dense.bin",
            "index.txt",
            "native_experts.txt",
            "tokenizer/tokenizer.json",
        ],
        "artifacts": {"native": {"kind": "file", "format": "gguf"}},
    }
    app = create_app(cfg, "a" * 32, "b" * 32, tmp_path / "state")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": "Bearer " + "a" * 32},
        ) as c:
            body = {
                "engine": "llama",
                "profile": "strata",
                "path": str(pack),
                "artifacts": {"native": str(root / "model.gguf")},
            }
            assert (await c.post("/api/models/inspect", json=body)).status_code == 200
            (pack / "native_experts.txt").write_text("0 12 7 0 1 2 3 4 ../outside.gguf\n")
            assert (await c.post("/api/models/inspect", json=body)).status_code == 422
    finally:
        await app.state.manager.close()


def gguf_metadata(architecture, context):
    def string(value):
        raw = value.encode()
        return struct.pack("<Q", len(raw)) + raw

    return (
        b"GGUF"
        + struct.pack("<IQQ", 3, 0, 2)
        + string("general.architecture")
        + struct.pack("<I", 8)
        + string(architecture)
        + string(architecture + ".context_length")
        + struct.pack("<II", 4, context)
    )


@pytest.mark.asyncio
async def test_gguf_metadata_bounds_profile_and_checks_architecture(tmp_path):
    cfg = registry_config(tmp_path)
    profile = cfg["engines"]["llama"]["profiles"]["gguf"]
    profile["supported_architectures"] = ["qwen3"]
    path = tmp_path / "models/model.gguf"
    path.write_bytes(gguf_metadata("qwen3", 4096))
    app = create_app(cfg, "a" * 32, "b" * 32, tmp_path / "state")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": "Bearer " + "a" * 32},
        ) as c:
            result = await c.post(
                "/api/models/inspect", json={"engine": "llama", "path": str(path)}
            )
            assert result.status_code == 200
            assert result.json()["summary"]["architecture"] == "qwen3"
            assert result.json()["summary"]["metadata_context"] == 4096
            assert result.json()["summary"]["context_limits"]["max"] == 4096
            path.write_bytes(gguf_metadata("unsupported", 4096))
            assert (
                await c.post("/api/models/inspect", json={"engine": "llama", "path": str(path)})
            ).status_code == 422
    finally:
        await app.state.manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata",
    [
        b"GGUF" + struct.pack("<IQQ", 3, 0, 10001),
        b"GGUF" + struct.pack("<IQQ", 3, 0, 1) + struct.pack("<Q", 2**63),
        b"GGUF" + struct.pack("<IQQ", 3, 0, 1) + struct.pack("<Q", 5) + b"x",
    ],
)
async def test_malformed_gguf_metadata_is_refused_with_bounded_work(tmp_path, metadata):
    cfg = registry_config(tmp_path)
    path = tmp_path / "models/model.gguf"
    path.write_bytes(metadata)
    app = create_app(cfg, "a" * 32, "b" * 32, tmp_path / "state")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": "Bearer " + "a" * 32},
        ) as c:
            assert (
                await c.post("/api/models/inspect", json={"engine": "llama", "path": str(path)})
            ).status_code == 422
    finally:
        await app.state.manager.close()
