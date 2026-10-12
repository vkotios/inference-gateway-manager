import asyncio
import sys

import pytest

from gateway.core import Manager


@pytest.mark.asyncio
async def test_force_unload_can_cancel_a_loading_backend(tmp_path):
    m = Manager(
        {
            "engines": {
                "one": {
                    "models": {
                        "fixture": {
                            "argv": [sys.executable, "-c", "import time; time.sleep(30)"],
                            "base_url": "http://127.0.0.1:1",
                        }
                    }
                }
            },
            "startup_timeout": 30,
        },
        tmp_path,
    )
    await m.select_engine("one")
    task = asyncio.create_task(m.load_model("fixture"))
    try:
        while m.phase != "loading":
            await asyncio.sleep(0.01)
        await asyncio.wait_for(m.unload(force=True), timeout=2)
        assert m.process is None
        assert m.model is None
        assert task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await m.close()
