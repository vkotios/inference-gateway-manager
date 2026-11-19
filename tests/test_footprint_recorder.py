import pytest

from gateway.footprint_recorder import (
    FootprintTracker,
    process_gpu_bytes,
    process_resident_bytes,
    system_used_bytes,
    tree_bytes,
)
from gateway.footprints import FootprintStore

MIB = 1024**2
GIB = 1024**3


def test_system_used_is_total_minus_available(tmp_path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 1000 kB\nMemFree: 1 kB\nMemAvailable: 400 kB\n")
    assert system_used_bytes(meminfo) == 600 * 1024
    meminfo.write_text("MemTotal: 1000 kB\n")
    with pytest.raises(ValueError):
        system_used_bytes(meminfo)


def test_resident_uses_pss_then_falls_back_to_rss(tmp_path):
    proc = tmp_path / "proc"
    (proc / "10").mkdir(parents=True)
    (proc / "10" / "smaps_rollup").write_text("Rss: 999 kB\nPss: 500 kB\n")
    assert process_resident_bytes(10, proc) == 500 * 1024
    (proc / "11").mkdir()
    (proc / "11" / "status").write_text("Name: x\nVmRSS: 300 kB\n")
    assert process_resident_bytes(11, proc) == 300 * 1024
    assert process_resident_bytes(12, proc) == 0  # vanished process


def test_gpu_memory_counts_each_drm_client_once(tmp_path):
    proc = tmp_path / "proc"
    fdinfo = proc / "10" / "fdinfo"
    fdinfo.mkdir(parents=True)
    body = "drm-driver:\tamdgpu\ndrm-client-id:\t{cid}\ndrm-memory-gtt:\t{kib} KiB\ndrm-memory-vram:\t999 KiB\n"
    (fdinfo / "5").write_text(body.format(cid=7, kib=1024))
    (fdinfo / "6").write_text(body.format(cid=7, kib=1024))  # duplicate descriptor, same client
    (fdinfo / "7").write_text(body.format(cid=8, kib=2048))
    (fdinfo / "8").write_text("pos:\t0\nflags:\t02\n")  # ordinary file descriptor
    assert process_gpu_bytes(10, proc) == (1024 + 2048) * 1024
    assert process_gpu_bytes(99, proc) == 0
    (proc / "10" / "smaps_rollup").write_text("Pss: 100 kB\n")
    assert tree_bytes([10], proc) == (100 * 1024, 3072 * 1024)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def make(tmp_path, used, tree, aux=lambda: 0, **kwargs):
    store = FootprintStore(tmp_path / "f.json")
    clock = Clock()
    tracker = FootprintTracker(
        store,
        sample_used=lambda: used["v"],
        sample_tree=lambda pids: tree["v"],
        pids=lambda: [1],
        aux_resident=aux,
        settle_seconds=30,
        rewrite_interval=60,
        clock=clock,
        **kwargs,
    )
    return tracker, store, clock


def test_records_after_settle_with_system_delta(tmp_path):
    used, tree = {"v": 10 * GIB}, {"v": (0, 0)}
    tracker, store, clock = make(tmp_path, used, tree)
    tracker.begin_load("e/m", "fp", 4096)
    used["v"] = 70 * GIB  # engine loaded: +60 GiB system-wide
    tree["v"] = (58 * GIB, 0)
    tracker.start()
    clock.now = 10
    tracker.tick()
    assert store.lookup("chat", "e/m", "fp", 4096) is None  # not settled yet
    clock.now = 31
    tracker.tick()
    record = store.lookup("chat", "e/m", "fp", 4096)
    assert record.steady_bytes == 60 * GIB and record.peak_bytes == 60 * GIB
    assert record.context == 4096 and record.source == "measured"


def test_memory_mapped_model_uses_process_total_when_system_change_is_small(tmp_path):
    """Page cache counts as available, so the system change can understate a mapped model."""
    used, tree = {"v": 10 * GIB}, {"v": (0, 0)}
    tracker, store, clock = make(tmp_path, used, tree)
    tracker.begin_load("e/m", "fp", 4096)
    used["v"] = 15 * GIB  # only +5 GiB visible system-wide
    tree["v"] = (50 * GIB, 8 * GIB)  # but the engine holds 58 GiB
    tracker.start()
    clock.now = 31
    tracker.tick()
    record = store.lookup("chat", "e/m", "fp", 4096)
    assert record.steady_bytes == 58 * GIB
    assert "counters differ" in record.note and "process total" in record.note


def test_peak_rises_and_is_rewritten_only_when_meaningful(tmp_path):
    used, tree = {"v": 0}, {"v": (0, 0)}
    tracker, store, clock = make(tmp_path, used, tree)
    tracker.begin_load("e/m", "fp", 4096)
    used["v"] = 50 * GIB
    tracker.start()
    clock.now = 31
    tracker.tick()
    assert store.lookup("chat", "e/m", "fp", 1).peak_bytes == 50 * GIB
    used["v"] = 50 * GIB + 10 * MIB  # tiny growth: no rewrite
    clock.now = 100
    tracker.tick()
    assert store.lookup("chat", "e/m", "fp", 1).peak_bytes == 50 * GIB
    used["v"] = 53 * GIB  # real growth after the rewrite interval
    clock.now = 200
    tracker.tick()
    record = store.lookup("chat", "e/m", "fp", 1)
    assert record.peak_bytes == 53 * GIB
    used["v"] = 51 * GIB  # usage falls: peak stays, steady follows
    clock.now = 300
    tracker.tick()
    tracker.flush("test")
    record = store.lookup("chat", "e/m", "fp", 1)
    assert record.peak_bytes == 53 * GIB and record.steady_bytes == 51 * GIB


def test_auxiliary_memory_is_not_attributed_to_the_chat_model(tmp_path):
    used, tree = {"v": 10 * GIB}, {"v": (0, 0)}
    aux = {"v": 0}
    tracker, store, clock = make(tmp_path, used, tree, aux=lambda: aux["v"])
    tracker.begin_load("e/m", "fp", 4096)
    used["v"] = 70 * GIB + 2 * GIB  # chat 60 GiB plus a 2 GiB worker started meanwhile
    aux["v"] = 2 * GIB
    tracker.start()
    clock.now = 31
    tracker.tick()
    assert store.lookup("chat", "e/m", "fp", 1).steady_bytes == 60 * GIB


def test_record_now_and_snapshot_and_missing_baseline(tmp_path):
    used, tree = {"v": 5 * GIB}, {"v": (20 * GIB, 0)}
    tracker, store, clock = make(tmp_path, used, tree)
    assert tracker.record_now() is None  # nothing loaded
    tracker.begin_load("e/m", "fp", 4096)
    tracker.start()
    assert tracker.record_now() is None  # no samples yet
    tracker.sample_once()
    record = tracker.record_now("calibrate")
    assert record.steady_bytes == 20 * GIB and "calibrate" in record.note
    snap = tracker.snapshot()
    assert snap["settled"] and snap["samples"] == 1 and snap["recorded"]["steady_bytes"] == 20 * GIB


def test_sampling_errors_do_not_crash_and_are_reported(tmp_path):
    used, tree = {"v": 0}, {"v": (0, 0)}
    tracker, store, clock = make(tmp_path, used, tree)
    tracker.begin_load("e/m", "fp", 4096)
    tracker.start()
    tracker.sample_used = lambda: (_ for _ in ()).throw(OSError("gone"))
    assert tracker.sample_once() is None
    assert "Memory sampling failed" in tracker.snapshot()["error"]


@pytest.mark.asyncio
async def test_stop_writes_final_peak_and_clears_state(tmp_path):
    used, tree = {"v": 0}, {"v": (0, 0)}
    tracker, store, clock = make(tmp_path, used, tree)
    tracker.begin_load("e/m", "fp", 4096)
    used["v"] = 40 * GIB
    tracker.start()
    clock.now = 31
    tracker.tick()
    used["v"] = 44 * GIB
    tracker.sample_once()
    await tracker.stop()
    assert store.lookup("chat", "e/m", "fp", 1).peak_bytes == 44 * GIB
    assert tracker.current is None and tracker.task is None
