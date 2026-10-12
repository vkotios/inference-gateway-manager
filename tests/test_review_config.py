import json
import signal
import subprocess
import sys
from pathlib import Path

from gateway.core import Manager, identity, signal_identity


def test_l5_example_strata_matcher_detects_python_module_launch(tmp_path):
    cfg = json.loads(
        (Path(__file__).resolve().parents[1] / "examples/config.example.json").read_text()
    )
    argv = cfg["engines"]["strata"]["models"]["qwen3.8-flash-next"]["argv"]
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", *argv[1:]])
    manager = Manager({"engines": {}, "native_matchers": cfg["native_matchers"]}, tmp_path)
    try:
        assert identity(process.pid) in manager.native_processes()
    finally:
        item = identity(process.pid)
        if item:
            signal_identity(item, signal.SIGKILL)
        process.wait(timeout=3)
        import asyncio

        asyncio.run(manager.http.aclose())
