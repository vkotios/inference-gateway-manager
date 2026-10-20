import json
from pathlib import Path

from gateway.config import validate


def test_gufo_has_safe_disabled_example_and_native_metadata_probe():
    cfg = validate(
        json.loads(
            (Path(__file__).resolve().parents[1] / "examples/config.example.json").read_text()
        )
    )
    gufo = cfg["engines"]["gufo"]
    spec = next(iter(gufo["models"].values()))
    assert spec["base_url"] == "http://127.0.0.1:8083"
    assert spec["ready_path"] == "/ready" and spec["probe"] == "slots"
    assert spec["enabled"] is False
    assert spec["argv"][spec["argv"].index("--served-model-name") + 1] == next(iter(gufo["models"]))
    assert spec["argv"][spec["argv"].index("--sessions") + 1] == "1"


async def test_gufo_fixture_native_slots_and_raw_stream(tmp_path):
    import socket
    import sys

    import httpx

    from gateway.app import create_app

    # Pinned upstream contract: 7b2784517f5652860e03c4ff0c61f2d8940139db,
    # src/cli/serve/http_server.cpp LlamaSlots and tests/functional/server_metrics.py.
    fixtures = Path(__file__).with_name("fixtures")
    (tmp_path / "slots.json").write_bytes((fixtures / "gufo-slots-idle.json").read_bytes())

    cfg = validate(
        json.loads(
            (Path(__file__).resolve().parents[1] / "examples/config.example.json").read_text()
        )
    )
    spec = cfg["engines"]["gufo"]["models"]["example-gufo"]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    spec.update(
        enabled=True,
        argv=[
            sys.executable,
            str(Path(__file__).with_name("fixture_backend.py")),
            str(port),
            str(tmp_path),
        ],
        base_url=f"http://127.0.0.1:{port}",
    )
    cfg.update(allowed_hosts=["testserver"], startup_timeout=5)
    app = create_app(cfg, "a" * 32, "b" * 32, tmp_path)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": "Bearer " + "a" * 32},
        ) as c:
            loaded = await c.post("/api/model", json={"engine": "gufo", "model": "example-gufo"})
            assert loaded.status_code == 200
            assert loaded.json()["active_settings"]["context"] == 4096
            assert loaded.json()["active_settings"]["context_source"] == "native_slots"
            assert loaded.json()["native_busy"] is False
            response = await c.post(
                "/v1/chat/completions",
                json={"model": "example-gufo", "messages": [], "stream": True},
            )
            assert response.content == b"data: fixture\n\ndata: [DONE]\n\n"
            (tmp_path / "slots.json").write_bytes((fixtures / "gufo-slots-busy.json").read_bytes())
            assert (await c.get("/api/status")).json()["native_busy"] is True
            assert (
                await c.post("/v1/chat/completions", json={"model": "example-gufo", "messages": []})
            ).status_code == 409
            assert (await c.post("/api/unload", json={})).status_code == 409
            (tmp_path / "slots.json").write_bytes((fixtures / "gufo-slots-idle.json").read_bytes())
            assert (await c.post("/api/unload", json={})).status_code == 200
    finally:
        await app.state.manager.close()


def test_gufo_profile_obeys_native_serve_llm_parser():
    import argparse

    from gateway.settings import select

    cfg = validate(
        json.loads(
            (Path(__file__).resolve().parents[1] / "examples/config.example.json").read_text()
        )
    )
    spec, _ = select(cfg["engines"]["gufo"]["profiles"]["gguf"], 4096)
    parser = argparse.ArgumentParser()
    serve = parser.add_subparsers(dest="action", required=True).add_parser("serve")
    serve.add_argument("--host", required=True)
    serve.add_argument("--port", type=int, required=True)
    serve.add_argument("--sessions", type=int, required=True)
    llm = serve.add_subparsers(dest="workflow", required=True).add_parser("llm")
    llm.add_argument("--model", required=True)
    llm.add_argument("--served-model-name", required=True)
    llm.add_argument("--context", type=int, required=True)
    parsed = parser.parse_args(spec["argv"][1:])
    assert parsed.sessions == 1 and parsed.port == 8083 and parsed.context == 4096
