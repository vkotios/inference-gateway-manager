import asyncio

import pytest

from gateway.memory_admission import MemoryAdmission, MemoryAdmissionError


@pytest.mark.asyncio
async def test_concurrent_admissions_reserve_once_and_release():
    admission = MemoryAdmission(lambda: 100, 30)
    async with admission.reserve(40):
        with pytest.raises(MemoryAdmissionError, match="headroom"):
            async with admission.reserve(40):
                pass
        assert admission.reserved_bytes == 40
    assert admission.reserved_bytes == 0
    async with admission.reserve(40):
        pass


@pytest.mark.asyncio
async def test_cancellation_releases_reservation():
    admission = MemoryAdmission(lambda: 100, 10)
    entered = asyncio.Event()

    async def work():
        async with admission.reserve(50):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(work())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert admission.reserved_bytes == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("sample", [None, -1, True, 3.5])
async def test_unknown_memory_fails_closed(sample):
    admission = MemoryAdmission(lambda: sample, 10)
    with pytest.raises(MemoryAdmissionError, match="measured"):
        async with admission.reserve(5):
            pass
    assert admission.reserved_bytes == 0


def test_effective_memory_respects_cgroup_ancestors(tmp_path):
    from gateway.memory_admission import effective_available_memory

    root = tmp_path / "cgroup"
    (root / "a" / "b").mkdir(parents=True)
    (root / "cgroup.controllers").write_text("memory")
    for path, limit, usage in ((root / "a", "100000", "40000"), (root / "a" / "b", "max", "1")):
        (path / "memory.max").write_text(limit)
        (path / "memory.current").write_text(usage)
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemAvailable: 100 kB\n")
    membership = tmp_path / "membership"
    membership.write_text("0::/a/b\n")
    assert (
        effective_available_memory(meminfo_path=meminfo, cgroup_path=membership, cgroup_mount=root)
        == 60000
    )
    (root / "memory.max").write_text("45000")
    (root / "memory.current").write_text("5000")
    assert (
        effective_available_memory(meminfo_path=meminfo, cgroup_path=membership, cgroup_mount=root)
        == 40000
    )
    (root / "a" / "memory.current").unlink()
    with pytest.raises(OSError):
        effective_available_memory(meminfo_path=meminfo, cgroup_path=membership, cgroup_mount=root)
