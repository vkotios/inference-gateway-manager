import socket
import sys
from pathlib import Path

import pytest

from gateway.core import Manager


@pytest.mark.asyncio
async def test_load_then_engine_switch_verifies_native_exit(tmp_path):
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
    m = Manager(
        {
            "engines": {"one": {"models": {"fixture": spec}}, "two": {"models": {}}},
            "native_matchers": [{"argv_contains": str(tmp_path)}],
            "startup_timeout": 5,
        },
        tmp_path,
    )
    await m.select_engine("one")
    try:
        await m.load_model("fixture")
        p = m.process
        assert p.poll() is None
        assert m.phase == "ready"
        await m.select_engine("two")
        assert p.poll() is not None
        assert m.model is None
        assert m.engine == "two"
        assert not m.native_processes()
    finally:
        await m.close()
