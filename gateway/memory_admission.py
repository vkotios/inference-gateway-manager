"""Fail-closed accounting for concurrent, bounded memory commitments.

The caller supplies an effective available-memory reading that accounts for all
applicable host and cgroup limits. This module does not launch or stop models.
"""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Callable


class MemoryAdmissionError(Exception):
    """Memory could not be verified or would fall below the required reserve."""


@dataclass(frozen=True)
class MemoryDecision:
    available_bytes: int
    reserved_bytes: int
    requested_bytes: int
    minimum_free_bytes: int


class _Reservation:
    __slots__ = ("bytes", "released")

    def __init__(self, size):
        self.bytes = size
        self.released = False


class MemoryAdmission:
    """Serialize reservations against a caller-provided effective memory sample.

    Reservations remain fully charged until released, even if the kernel sample
    already reflects some allocations. This can reject safe work but avoids
    assuming that a reservation has materialized in a particular measurement.
    """

    def __init__(self, sample_available: Callable[[], int | None], minimum_free_bytes: int):
        if type(minimum_free_bytes) is not int or minimum_free_bytes <= 0:
            raise ValueError("minimum_free_bytes must be a positive integer")
        self._sample = sample_available
        self.minimum_free_bytes = minimum_free_bytes
        self._lock = asyncio.Lock()
        self._reserved = 0

    @property
    def reserved_bytes(self):
        return self._reserved

    async def acquire(self, requested_bytes: int):
        """Reserve memory until ``release``; returns ``(decision, token)``.

        Unlike ``reserve`` the reservation is not tied to a ``with`` block, so it can
        outlive a lock that was needed only for the admission decision. Callers must
        release in a ``finally`` clause.
        """
        if type(requested_bytes) is not int or requested_bytes <= 0:
            raise ValueError("requested_bytes must be a positive integer")
        async with self._lock:
            try:
                available = self._sample()
            except (OSError, ValueError, RuntimeError) as exc:
                raise MemoryAdmissionError("Available memory could not be measured") from exc
            if type(available) is not int or available < 0:
                raise MemoryAdmissionError("Available memory could not be measured")
            decision = MemoryDecision(
                available, self._reserved, requested_bytes, self.minimum_free_bytes
            )
            if available - self._reserved - requested_bytes < self.minimum_free_bytes:
                raise MemoryAdmissionError("Insufficient memory headroom")
            self._reserved += requested_bytes
            return decision, _Reservation(requested_bytes)

    def release(self, token):
        """Idempotent; mutations run atomically on the event-loop thread."""
        if not token.released:
            token.released = True
            self._reserved -= token.bytes

    @asynccontextmanager
    async def reserve(self, requested_bytes: int):
        decision, token = await self.acquire(requested_bytes)
        try:
            yield decision
        finally:
            # No await is needed: mutations execute atomically on the event-loop
            # thread, including when the operation is cancelled.
            self.release(token)


def effective_available_memory(
    *, meminfo_path="/proc/meminfo", cgroup_path="/proc/self/cgroup", cgroup_mount="/sys/fs/cgroup"
):
    """Read physical headroom and every visible cgroup-v2 ancestor limit.

    Unknown or hidden controllers raise rather than permitting an unsafe load.
    Swap is deliberately excluded. The mount must expose the unified hierarchy
    from its root; deployments with a different mount layout must configure an
    equivalent reader rather than silently assuming unlimited memory.
    """
    from pathlib import Path

    lines = Path(meminfo_path).read_text().splitlines()
    candidates = [line.split() for line in lines if line.startswith("MemAvailable:")]
    if len(candidates) != 1 or len(candidates[0]) != 3 or candidates[0][2] != "kB":
        raise ValueError("MemAvailable unavailable")
    available = int(candidates[0][1]) * 1024
    for limit, usage in _cgroup_limits(cgroup_path, cgroup_mount):
        if limit is not None:
            available = min(available, max(0, limit - usage))
    return available


def _cgroup_limits(cgroup_path, cgroup_mount):
    """``(limit | None, usage)`` for the process's cgroup and every ancestor.

    Raises rather than guessing when the hierarchy cannot be read completely.
    """
    from pathlib import Path

    entries = [line.split(":", 2) for line in Path(cgroup_path).read_text().splitlines()]
    unified = [fields[2] for fields in entries if len(fields) == 3 and fields[:2] == ["0", ""]]
    if len(unified) != 1 or not unified[0].startswith("/"):
        raise ValueError("Unified cgroup path unavailable")
    parts = Path(unified[0]).parts[1:]
    if any(part in {"..", "."} for part in parts):
        raise ValueError("Invalid cgroup path")
    root = Path(cgroup_mount)
    if not (root / "cgroup.controllers").is_file():
        raise ValueError("Unified cgroup mount unavailable")
    current = root
    paths = [root]
    for part in parts:
        current = current / part
        paths.append(current)
    result = []
    for current in paths:
        # The host hierarchy root may not expose memory.max/current. A
        # namespaced cgroup root can expose both and may itself be limited.
        if current == root and not (current / "memory.max").exists():
            if (current / "memory.current").exists():
                raise ValueError("Incomplete root cgroup memory controller")
            continue
        limit_text = (current / "memory.max").read_text().strip()
        usage_text = (current / "memory.current").read_text().strip()
        usage = int(usage_text)
        if usage < 0:
            raise ValueError("Invalid cgroup usage")
        limit = None
        if limit_text != "max":
            limit = int(limit_text)
            if limit < 0:
                raise ValueError("Invalid cgroup limit")
        result.append((limit, usage))
    return result


def effective_total_memory(
    *, meminfo_path="/proc/meminfo", cgroup_path="/proc/self/cgroup", cgroup_mount="/sys/fs/cgroup"
):
    """Physical memory capped by the tightest visible cgroup limit (swap excluded)."""
    from pathlib import Path

    lines = Path(meminfo_path).read_text().splitlines()
    candidates = [line.split() for line in lines if line.startswith("MemTotal:")]
    if len(candidates) != 1 or len(candidates[0]) != 3 or candidates[0][2] != "kB":
        raise ValueError("MemTotal unavailable")
    total = int(candidates[0][1]) * 1024
    for limit, _usage in _cgroup_limits(cgroup_path, cgroup_mount):
        if limit is not None:
            total = min(total, limit)
    return total
