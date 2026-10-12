import asyncio
import importlib.util
import os
import socket
import uuid

import pytest


@pytest.mark.asyncio
async def test_watchdog_notifies_real_systemd_socket(monkeypatch, tmp_path):
    assert importlib.util.find_spec("gateway.supervision") is not None, "Watchdog support missing"
    from gateway.supervision import watchdog

    path = "@igm-test-" + uuid.uuid4().hex
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
        receiver.bind("\0" + path[1:])
        receiver.setblocking(False)
        monkeypatch.setenv("NOTIFY_SOCKET", path)
        monkeypatch.setenv("WATCHDOG_USEC", "300000")
        monkeypatch.setenv("WATCHDOG_PID", str(os.getpid()))
        task = asyncio.create_task(watchdog())
        try:
            loop = asyncio.get_running_loop()
            first = await asyncio.wait_for(loop.sock_recv(receiver, 4096), 1)
            second = await asyncio.wait_for(loop.sock_recv(receiver, 4096), 1)
            assert first == b"READY=1"
            assert second == b"WATCHDOG=1"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
