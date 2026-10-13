"""Reproduce a native process appearing between ownership and conflict scans."""

import os
import subprocess
import sys

import pytest
from fastapi import HTTPException
from test_review_lifecycle import config, reap

from gateway.core import Manager, identity


@pytest.mark.asyncio
@pytest.mark.parametrize("tag", ["current-launch", "wrong-launch", None])
async def test_startup_verifies_the_same_native_snapshot(tmp_path, monkeypatch, tag):
    manager = Manager(config(tmp_path), tmp_path)
    await manager.select_engine("one")
    original_capture = manager.capture_owned
    original_scan = manager.native_processes
    capturing = False
    late_process = None
    late_identity = None

    def capture(*args, **kwargs):
        nonlocal capturing
        capturing = True
        try:
            return original_capture(*args, **kwargs)
        finally:
            capturing = False

    def scan():
        nonlocal late_process, late_identity
        # Introduce a real detached process in the conflict-check seam, not
        # inside capture_owned's earlier enumeration. This makes the race
        # deterministic without replacing identity or ownership verification.
        if manager.phase == "loading" and not capturing and late_process is None:
            env = {k: v for k, v in os.environ.items() if not k.startswith("IGM_")}
            if tag is not None:
                env["IGM_ENGINE_RUN_ID"] = manager.launch_id if tag == "current-launch" else tag
            late_process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import time; time.sleep(30)",
                    manager.config["native_matchers"][0]["argv_contains"],
                ],
                env=env,
                start_new_session=True,
            )
            late_identity = identity(late_process.pid)
        return original_scan()

    monkeypatch.setattr(manager, "capture_owned", capture)
    monkeypatch.setattr(manager, "native_processes", scan)
    try:
        if tag == "current-launch":
            await manager.load_model("fixture")
            assert manager.phase == "ready"
            assert late_identity in manager.owned
            await manager.unload(force=True)
            assert late_process.poll() is not None
            assert not manager.owned
        else:
            with pytest.raises((RuntimeError, HTTPException)):
                await manager.load_model("fixture")
            assert late_identity not in manager.owned
            assert late_process.poll() is None, "An unrelated process was signalled"
    finally:
        if late_process is not None:
            manager.owned.discard(late_identity)
            reap(late_process)
        await manager.close()
