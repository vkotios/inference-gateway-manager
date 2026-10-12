"""Optional systemd readiness and event-loop watchdog (no external dependency)."""

import asyncio
import os
import socket


def notify(address, message):
    if address.startswith("@"):
        address = "\0" + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
        client.setblocking(False)
        client.sendto(message.encode("ascii"), address)


async def watchdog():
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    notify(address, "READY=1")
    interval = int(os.environ.get("WATCHDOG_USEC", "0")) / 1000000
    pid = int(os.environ.get("WATCHDOG_PID", str(os.getpid())))
    if interval <= 0 or pid != os.getpid():
        return
    while True:
        notify(address, "WATCHDOG=1")
        await asyncio.sleep(interval / 3)
