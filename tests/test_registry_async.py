"""Slow artifact disks and disconnected callers must not freeze the control plane."""

import asyncio
import threading

import httpx
import pytest

from gateway.app import create_app


@pytest.mark.asyncio
async def test_slow_model_inspection_does_not_block_health_or_other_controls(tmp_path, monkeypatch):
    app = create_app(
        {"allowed_hosts": ["testserver"], "engines": {"one": {"models": {}}}},
        "a" * 32,
        "b" * 32,
        tmp_path,
    )
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def inspect(payload):
        started.set()
        release.wait(1)
        finished.set()
        return {"engine": "one", "model": "new"}, {"context": 4096}, {}

    monkeypatch.setattr(app.state.registry, "inspect", inspect)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": "Bearer " + "a" * 32},
        ) as client:
            pending = asyncio.create_task(client.post("/api/models/inspect", json={}))
            try:
                assert await asyncio.to_thread(started.wait, 1)
                assert not finished.is_set(), "Artifact inspection blocked the event loop"
                assert not pending.done(), "Artifact inspection blocked the event loop"
                assert (await client.get("/health")).status_code == 200
                assert (await client.get("/api/status")).status_code == 200
            finally:
                release.set()
                await pending
    finally:
        await app.state.manager.close()


@pytest.mark.asyncio
async def test_cancelled_registry_writer_finishes_install_before_next_writer(tmp_path, monkeypatch):
    app = create_app({"engines": {"one": {"models": {}}}}, "a" * 32, "b" * 32, tmp_path)
    started, release = threading.Event(), threading.Event()
    calls = []

    def persist(payload):
        calls.append(payload["model"])
        started.set()
        release.wait(2)
        return {"engine": "one", "model": payload["model"]}, {"context": 4096}

    monkeypatch.setattr(app.state.registry, "persist", persist, raising=False)
    endpoint = next(
        route.endpoint for route in app.routes if getattr(route, "path", None) == "/api/models"
    )
    one = asyncio.create_task(endpoint({"model": "first"}))
    two = None
    try:
        assert await asyncio.to_thread(started.wait, 0.5), (
            "Persistence was not isolated from installation"
        )
        one.cancel()
        await asyncio.sleep(0.01)
        two = asyncio.create_task(endpoint({"model": "second"}))
        await asyncio.sleep(0.02)
        assert calls == ["first"], "Cancellation released the writer lock before disk work ended"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await one
        assert (await two)["id"] == "second"
        assert set(app.state.manager.config["engines"]["one"]["models"]) == {"first", "second"}
    finally:
        release.set()
        await asyncio.gather(*[t for t in (one, two) if t is not None], return_exceptions=True)
        await app.state.manager.close()


@pytest.mark.asyncio
async def test_cancelled_removal_finishes_before_a_new_registration(tmp_path, monkeypatch):
    import json

    from test_registry import registry_config

    app = create_app(registry_config(tmp_path), "a" * 32, "b" * 32, tmp_path / "state")
    registry = app.state.registry
    record = {"engine": "llama", "model": "first", "path": str(tmp_path / "models/model.gguf")}
    registry.register(record)
    started, release = threading.Event(), threading.Event()
    persist_remove = registry.persist_remove

    def delayed_remove(engine, model):
        started.set()
        release.wait(2)
        persist_remove(engine, model)

    monkeypatch.setattr(registry, "persist_remove", delayed_remove)
    remove = next(
        r.endpoint for r in app.routes if getattr(r, "path", None) == "/api/models/{engine}/{model}"
    )
    add = next(r.endpoint for r in app.routes if getattr(r, "path", None) == "/api/models")
    first = asyncio.create_task(remove("llama", "first"))
    second = None
    try:
        assert await asyncio.to_thread(started.wait, 1)
        first.cancel()
        await asyncio.sleep(0)
        second = asyncio.create_task(add({**record, "model": "second"}))
        await asyncio.sleep(0)
        assert not second.done()
        assert "first" in app.state.manager.config["engines"]["llama"]["models"]
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert (await second)["id"] == "second"
        assert set(app.state.manager.config["engines"]["llama"]["models"]) == {"second"}
        assert [r["model"] for r in json.loads(registry.path.read_text())] == ["second"]
    finally:
        release.set()
        await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
        await app.state.manager.close()
