import os
import signal
import subprocess
import sys

import pytest
from fastapi import HTTPException

from gateway.core import Manager, identity, signal_identity


def test_signal_uses_pidfd_instead_of_racy_numeric_kill(monkeypatch):
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        saved = identity(p.pid)

        def forbidden(*args):
            raise AssertionError("Numeric PID kill leaves a reuse race")

        monkeypatch.setattr(os, "kill", forbidden)
        signal_identity(saved, signal.SIGTERM)
        p.wait(timeout=2)
        assert p.returncode == -signal.SIGTERM
    finally:
        monkeypatch.undo()
        if p.poll() is None:
            p.terminate()
            p.wait()


def test_stale_identity_does_not_signal_disposable_process():
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        saved = identity(p.pid)
        signal_identity((saved[0], saved[1] + 1), signal.SIGKILL)
        assert p.poll() is None
    finally:
        p.terminate()
        p.wait()


@pytest.mark.asyncio
async def test_unknown_native_occupancy_blocks_normal_switch(tmp_path):
    m = Manager(
        {
            "engines": {
                "one": {"models": {"fixture": {"base_url": "http://127.0.0.1:1"}}},
                "two": {"models": {}},
            }
        },
        tmp_path,
    )
    m.engine, m.model, m.phase = "one", "fixture", "ready"
    m.process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    m.capture_owned()
    try:
        with pytest.raises(HTTPException) as exc:
            await m.select_engine("two")
        assert exc.value.status_code == 409
        assert m.process.poll() is None
        await m.select_engine("two", force=True)
        assert m.process is None
        assert m.engine == "two"
    finally:
        await m.close()
