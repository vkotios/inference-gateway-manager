"""Measure what a running model costs the host, without trusting a single counter.

Two independent views are combined and the larger is recorded:

* the system-wide increase of used memory since just before the model was launched, and
* the engine's own total: resident memory of its process set (proportional set size, so
  memory-mapped weights count) plus GPU memory the driver reports for those processes.

``MemAvailable`` alone is not used for the footprint: it counts reclaimable page cache as
available, so a memory-mapped model can look almost free. A high-water mark is kept while
the model stays loaded, so the record rises if real use allocates more later.
"""

import asyncio
import re
import time
from pathlib import Path

from .footprints import CHAT, Footprint


def system_used_bytes(meminfo_path="/proc/meminfo"):
    """``MemTotal - MemAvailable`` (swap excluded)."""
    fields = {}
    for line in Path(meminfo_path).read_text().splitlines():
        name, _, rest = line.partition(":")
        parts = rest.split()
        if len(parts) == 2 and parts[1] == "kB":
            fields[name] = int(parts[0]) * 1024
    if "MemTotal" not in fields or "MemAvailable" not in fields:
        raise ValueError("MemTotal/MemAvailable unavailable")
    return max(0, fields["MemTotal"] - fields["MemAvailable"])


def process_resident_bytes(pid, proc="/proc"):
    """Proportional set size (counts mapped files fairly); falls back to VmRSS."""
    base = Path(proc) / str(pid)
    try:
        for line in (base / "smaps_rollup").read_text().splitlines():
            if line.startswith("Pss:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    try:
        for line in (base / "status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


_UNIT = {"KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "B": 1}


def process_gpu_bytes(pid, proc="/proc"):
    """Host-visible GPU memory (GTT) the driver reports for this process, via fdinfo.

    Several descriptors can share one DRM client; each client is counted once.
    Missing or unreadable fdinfo yields 0 rather than an error.
    """
    fdinfo = Path(proc) / str(pid) / "fdinfo"
    clients = {}
    try:
        entries = list(fdinfo.iterdir())
    except OSError:
        return 0
    for entry in entries:
        try:
            text = entry.read_text()
        except OSError:
            continue
        if "drm-driver" not in text:
            continue
        client = re.search(r"^drm-client-id:\s*(\d+)", text, re.M)
        match = re.search(r"^drm-memory-gtt:\s*(\d+)\s*(\w+)?", text, re.M)
        if not match:
            continue
        size = int(match.group(1)) * _UNIT.get(match.group(2) or "KiB", 1024)
        clients[client.group(1) if client else str(entry)] = size
    return sum(clients.values())


def tree_bytes(pids, proc="/proc"):
    """``(resident, gpu)`` summed over a process set."""
    resident = gpu = 0
    for pid in pids:
        resident += process_resident_bytes(pid, proc)
        gpu += process_gpu_bytes(pid, proc)
    return resident, gpu


class FootprintTracker:
    """Samples the loaded chat engine and records its footprint when it settles."""

    def __init__(
        self,
        store,
        *,
        sample_used=system_used_bytes,
        sample_tree=tree_bytes,
        pids=lambda: [],
        aux_resident=lambda: 0,
        interval=5.0,
        settle_seconds=30.0,
        rewrite_interval=60.0,
        clock=time.monotonic,
    ):
        self.store = store
        self.sample_used = sample_used
        self.sample_tree = sample_tree
        self.pids = pids
        self.aux_resident = aux_resident
        self.interval = interval
        self.settle_seconds = settle_seconds
        self.rewrite_interval = rewrite_interval
        self.clock = clock
        self.task = None
        self.pending = None  # captured before launch
        self.current = None
        self.error = None

    # -- before / after launch -------------------------------------------------------

    def begin_load(self, key, fingerprint, context):
        """Capture the baseline immediately before an engine launches."""
        try:
            baseline = max(0, self.sample_used() - self.aux_resident())
        except (OSError, ValueError):
            baseline = None
        self.pending = {
            "key": key,
            "fingerprint": fingerprint,
            "context": context,
            "baseline": baseline,
        }

    def start(self):
        """Begin sampling once the engine reports ready."""
        if self.pending is None:
            return
        self.stop_sync()
        self.current = {
            **self.pending,
            "ready_at": self.clock(),
            "peak_delta": 0,
            "peak_tree": 0,
            "last_delta": 0,
            "last_tree": 0,
            "settled": False,
            "flushed_peak": 0,
            "flushed_at": None,
            "samples": 0,
            "record": None,
        }
        self.pending = None
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # sampling driven manually (tests)
        self.task = asyncio.create_task(self._run())

    async def _run(self):
        while True:
            await asyncio.sleep(self.interval)
            self.tick()

    def stop_sync(self):
        if self.task is not None:
            self.task.cancel()
            self.task = None

    def finish(self):
        """Stop sampling and write the final high-water mark (synchronous)."""
        self.stop_sync()
        if self.current is not None and self.current["settled"]:
            self.flush("final")
        self.current = None
        self.pending = None

    async def stop(self):
        self.finish()

    # -- measurement -----------------------------------------------------------------

    def sample_once(self):
        cur = self.current
        if cur is None:
            return None
        try:
            used = self.sample_used()
            resident, gpu = self.sample_tree(list(self.pids()))
        except (OSError, ValueError) as exc:
            self.error = f"Memory sampling failed: {type(exc).__name__}"
            return None
        self.error = None
        baseline = cur["baseline"]
        delta = max(0, used - baseline - self.aux_resident()) if baseline is not None else 0
        tree = resident + gpu
        cur["last_delta"], cur["last_tree"] = delta, tree
        cur["peak_delta"] = max(cur["peak_delta"], delta)
        cur["peak_tree"] = max(cur["peak_tree"], tree)
        cur["samples"] += 1
        return delta, tree

    def tick(self):
        """One sampling step plus the settle / periodic write policy."""
        cur = self.current
        if cur is None or self.sample_once() is None:
            return
        now = self.clock()
        peak = max(cur["peak_delta"], cur["peak_tree"])
        if not cur["settled"]:
            if now - cur["ready_at"] >= self.settle_seconds:
                cur["settled"] = True
                self.flush("settled")
            return
        grew = peak - cur["flushed_peak"] >= max(256 * 1024**2, cur["flushed_peak"] // 100)
        if grew and now - cur["flushed_at"] >= self.rewrite_interval:
            self.flush("peak grew")

    def measured(self):
        """``(steady, peak, note)`` from the current samples; None before any sample."""
        cur = self.current
        if cur is None or cur["samples"] == 0:
            return None
        steady = max(cur["last_delta"], cur["last_tree"])
        peak = max(cur["peak_delta"], cur["peak_tree"], steady)
        notes = []
        low, high = sorted((cur["peak_delta"], cur["peak_tree"]))
        if high and low < 0.75 * high:
            which = "process total" if cur["peak_tree"] >= cur["peak_delta"] else "system change"
            notes.append(
                f"counters differ (system change {cur['peak_delta'] >> 20} MiB, "
                f"process total {cur['peak_tree'] >> 20} MiB); recorded the larger ({which})"
            )
        if cur["baseline"] is None:
            notes.append("no pre-launch baseline; system change unavailable")
        return steady, peak, "; ".join(notes)

    def flush(self, reason):
        cur = self.current
        result = self.measured()
        if cur is None or result is None:
            return None
        steady, peak, note = result
        if steady <= 0:
            return None
        footprint = Footprint(
            CHAT,
            cur["key"],
            steady,
            peak,
            0,
            cur["context"],
            cur["fingerprint"],
            "measured",
            time.time(),
            f"{reason}{'; ' + note if note else ''}",
        )
        cur["record"] = self.store.record(footprint)
        cur["flushed_peak"] = peak
        cur["flushed_at"] = self.clock()
        return cur["record"]

    def record_now(self, note="recorded by operator"):
        """Operator action: write the current high-water mark immediately."""
        cur = self.current
        if cur is None or cur["samples"] == 0:
            return None
        cur["settled"] = True
        return self.flush(note)

    def snapshot(self):
        cur = self.current
        if cur is None:
            return None
        peak = max(cur["peak_delta"], cur["peak_tree"])
        return {
            "key": cur["key"],
            "context": cur["context"],
            "settled": cur["settled"],
            "samples": cur["samples"],
            "peak_bytes": peak,
            "recorded": cur["record"].public() if cur["record"] else None,
            "error": self.error,
        }
