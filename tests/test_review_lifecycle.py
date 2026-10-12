"""Regression reproductions use only disposable processes, never real engines."""

import asyncio
import signal
import socket
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

from gateway.app import create_app
from gateway.core import Manager, identity, signal_identity


@pytest.mark.asyncio
async def test_s2_backend_environment_never_contains_gateway_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("IGM_CONTROL_KEY", "synthetic-admin-not-a-real-key")
    monkeypatch.setenv("IGM_INFERENCE_KEY", "synthetic-inference-not-a-real-key")
    monkeypatch.setenv("NOTIFY_SOCKET", "@synthetic-notify")
    monkeypatch.setenv("WATCHDOG_USEC", "30000000")
    monkeypatch.setenv("WATCHDOG_PID", "123")
    cfg = config(tmp_path, ready=False)
    cfg["engines"]["one"]["models"]["fixture"]["env"] = {
        "IGM_CONTROL_KEY": "also-forbidden",
        "BACKEND_OPTION": "preserved",
    }
    manager = Manager(cfg, tmp_path)
    await manager.select_engine("one")
    load = asyncio.create_task(manager.load_model("fixture"))
    try:
        await wait_for(lambda: manager.process is not None)
        entries = Path(f"/proc/{manager.process.pid}/environ").read_bytes().split(b"\0")
        names = {entry.split(b"=", 1)[0] for entry in entries}
        assert b"IGM_CONTROL_KEY" not in names
        assert b"IGM_INFERENCE_KEY" not in names
        assert b"BACKEND_OPTION=preserved" in entries
        assert b"IGM_ENGINE_RUN_ID" in names
        assert not names & {b"NOTIFY_SOCKET", b"WATCHDOG_USEC", b"WATCHDOG_PID"}
    finally:
        load.cancel()
        await asyncio.gather(load, return_exceptions=True)
        await manager.close()


def config(tmp_path, *, ready=True):
    marker = str(tmp_path / "native-marker")
    if ready:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        argv = [
            sys.executable,
            str(Path(__file__).with_name("fixture_backend.py")),
            str(port),
            marker,
        ]
        base_url = f"http://127.0.0.1:{port}"
    else:
        argv = [sys.executable, "-c", "import time; time.sleep(30)", marker]
        base_url = "http://127.0.0.1:1"
    spec = {"argv": argv, "base_url": base_url, "probe": "slots"}
    return {
        "allowed_hosts": ["testserver"],
        "engines": {"one": {"models": {"fixture": spec}}},
        "native_matchers": [{"argv_contains": marker}],
        "startup_timeout": 5,
        "stop_timeout": 0.1,
    }


async def wait_for(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.01)


def reap(process):
    item = identity(process.pid)
    if item:
        signal_identity(item, signal.SIGKILL)
    process.wait(timeout=3)


@pytest.mark.asyncio
async def test_s1_external_match_during_startup_is_never_owned_or_signalled(tmp_path):
    cfg = config(tmp_path, ready=False)
    manager = Manager(cfg, tmp_path)
    await manager.select_engine("one")
    load = asyncio.create_task(manager.load_model("fixture"))
    external = None
    try:
        await wait_for(lambda: manager.phase == "loading")
        external = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import time; time.sleep(30)",
                cfg["native_matchers"][0]["argv_contains"],
            ]
        )
        item = identity(external.pid)
        await asyncio.sleep(0.35)
        assert item not in manager.owned
        assert external.poll() is None
        await wait_for(load.done)
        assert isinstance(load.exception(), (HTTPException, RuntimeError))
    finally:
        # Refusal boundary: the pre-fix cleanup must not signal the outsider.
        if external:
            manager.owned.discard(identity(external.pid))
            reap(external)
        load.cancel()
        await asyncio.gather(load, return_exceptions=True)
        await manager.close()


@pytest.mark.asyncio
async def test_l1_repeated_cancel_keeps_lifecycle_lock_until_cleanup_finishes(
    tmp_path, monkeypatch
):
    manager = Manager(config(tmp_path, ready=False), tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_probe():
        entered.set()
        await release.wait()
        return False

    monkeypatch.setattr(manager, "native_busy", slow_probe)
    await manager.select_engine("one")
    load = asyncio.create_task(manager.load_model("fixture"))
    try:
        await wait_for(lambda: manager.phase == "loading")
        load.cancel()
        await asyncio.wait_for(entered.wait(), 3)
        load.cancel()
        await asyncio.sleep(0.05)
        assert manager.lock.locked(), "Second cancellation let cleanup escape the lock"
    finally:
        release.set()
        await asyncio.gather(load, return_exceptions=True)
        await manager.close()


@pytest.mark.asyncio
async def test_load_reserves_admission_before_internal_task_acquires_lock(tmp_path, monkeypatch):
    app = create_app(config(tmp_path), "a" * 32, "b" * 32, tmp_path)
    manager = app.state.manager
    await manager.select_engine("one")
    entered, release = asyncio.Event(), asyncio.Event()
    original = manager._load_model

    async def paused(*args):
        entered.set()
        await release.wait()
        return await original(*args)

    monkeypatch.setattr(manager, "_load_model", paused)
    first = second = None
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": "Bearer " + "a" * 32},
        ) as client:
            first = asyncio.create_task(client.post("/api/model", json={"model": "fixture"}))
            await asyncio.wait_for(entered.wait(), 3)
            second = asyncio.create_task(client.post("/api/model", json={"model": "fixture"}))
            await asyncio.sleep(0.05)
            assert second.done(), "A competing load queued in the managed-task scheduling gap"
            assert (await second).status_code == 409
            release.set()
            assert (await first).status_code == 200
    finally:
        release.set()
        await asyncio.gather(*(task for task in (first, second) if task), return_exceptions=True)
        await manager.close()


@pytest.mark.asyncio
async def test_s1_exited_launcher_never_claims_a_reused_identity(tmp_path, monkeypatch):
    import gateway.core as core

    manager = Manager(config(tmp_path, ready=False), tmp_path)
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    manager.process = process
    manager.capture_owned()
    saved = identity(process.pid)
    reap(process)
    actual_identity = core.identity
    reused = (saved[0], saved[1] + 1)
    try:
        with monkeypatch.context() as patcher:
            patcher.setattr(
                core, "identity", lambda pid: reused if pid == process.pid else actual_identity(pid)
            )
            manager.capture_owned()
            assert reused not in manager.owned
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_s1_dead_launch_session_does_not_claim_an_unrelated_new_session(
    tmp_path, monkeypatch
):
    import gateway.core as core

    manager = Manager(config(tmp_path, ready=False), tmp_path)
    launcher = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
    )
    external = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
    )
    manager.process = launcher
    manager.capture_owned()
    reap(launcher)
    outside = identity(external.pid)
    original_getsid = core.os.getsid
    try:
        with monkeypatch.context() as patcher:
            patcher.setattr(
                core.os,
                "getsid",
                lambda pid: launcher.pid if pid == external.pid else original_getsid(pid),
            )
            manager.capture_owned()
            assert outside not in manager.owned
    finally:
        manager.owned.discard(outside)
        reap(external)
        await manager.close()


@pytest.mark.asyncio
async def test_s1_detached_child_with_launch_tag_is_still_cleaned_up(tmp_path):
    child_file = tmp_path / "child.pid"
    cfg = config(tmp_path, ready=False)
    marker = cfg["native_matchers"][0]["argv_contains"]
    code = "import subprocess,sys,time; from pathlib import Path; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)',sys.argv[2]], start_new_session=True); Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(30)"
    cfg["engines"]["one"]["models"]["fixture"]["argv"] = [
        sys.executable,
        "-c",
        code,
        str(child_file),
        marker,
    ]
    manager = Manager(cfg, tmp_path)
    await manager.select_engine("one")
    load = asyncio.create_task(manager.load_model("fixture"))
    child = None
    try:
        await wait_for(child_file.exists)
        child = identity(int(child_file.read_text()))
        await wait_for(lambda: child in manager.owned)
        await manager.unload(force=True)
        assert identity(child[0]) != child
        assert not manager.owned
        assert manager.process is None
    finally:
        load.cancel()
        await asyncio.gather(load, return_exceptions=True)
        if child and identity(child[0]) == child:
            signal_identity(child, signal.SIGKILL)
        await manager.close()


@pytest.mark.asyncio
async def test_l3_permission_denial_returns_503_preserves_ownership_and_can_retry(
    tmp_path, monkeypatch
):
    app = create_app(config(tmp_path), "a" * 32, "b" * 32, tmp_path)
    manager = app.state.manager
    await manager.select_engine("one")
    await manager.load_model("fixture")
    process = manager.process
    saved = identity(process.pid)

    def deny(*args):
        raise PermissionError("synthetic refusal on disposable fixture only")

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://testserver",
            headers={"Authorization": "Bearer " + "a" * 32},
        ) as client:
            with monkeypatch.context() as patcher:
                patcher.setattr(signal, "pidfd_send_signal", deny)
                result = await client.post("/api/unload", json={"force": True})
            assert result.status_code == 503
            assert manager.phase == "error"
            assert saved in manager.owned
            assert process.poll() is None
            retry = await client.post("/api/unload", json={"force": True})
            assert retry.status_code == 200
            assert process.poll() is not None
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_l10_failed_spawn_unload_clears_model_and_error(tmp_path):
    cfg = config(tmp_path)
    cfg["engines"]["one"]["models"]["fixture"]["argv"] = [str(tmp_path / "missing-backend")]
    app = create_app(cfg, "a" * 32, "b" * 32, tmp_path)
    manager = app.state.manager
    await manager.select_engine("one")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": "Bearer " + "a" * 32},
        ) as client:
            result = await client.post("/api/model", json={"model": "fixture"})
            assert result.status_code == 503
            result = await client.post("/api/unload", json={})
            assert result.status_code == 200
            assert result.json()["phase"] == "selected"
            assert result.json()["loaded_model"] is None
            assert result.json()["error"] is None
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_l2_crashed_backend_status_and_nonforce_reload(tmp_path):
    app = create_app(config(tmp_path), "a" * 32, "b" * 32, tmp_path)
    manager = app.state.manager
    await manager.select_engine("one")
    await manager.load_model("fixture")
    process = manager.process
    reap(process)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": "Bearer " + "a" * 32},
        ) as client:
            state = (await client.get("/api/status")).json()
            assert state["phase"] == "error"
            assert state["loaded_model"] is None
            assert (await client.get("/v1/models")).json()["data"][0]["status"] == "unloaded"
            response = await client.post("/api/model", json={"model": "fixture"})
            assert response.status_code == 200
            assert response.json()["phase"] == "ready"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_l6_http_force_cancel_returns_409_and_selected_state(tmp_path):
    app = create_app(config(tmp_path, ready=False), "a" * 32, "b" * 32, tmp_path)
    manager = app.state.manager
    await manager.select_engine("one")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
        headers={"Authorization": "Bearer " + "a" * 32},
    ) as client:
        load = asyncio.create_task(client.post("/api/model", json={"model": "fixture"}))
        try:
            await wait_for(lambda: manager.phase == "loading")
            response = await client.post("/api/unload", json={"force": True})
            cancelled = await load
            assert cancelled.status_code == 409
            assert response.status_code == 200
            assert response.json()["phase"] == "selected"
            assert response.json()["error"] is None
        finally:
            await asyncio.gather(load, return_exceptions=True)
            await manager.close()
