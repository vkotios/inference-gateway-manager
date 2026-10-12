import os
from pathlib import Path

import pytest

from gateway.core import Manager


@pytest.mark.parametrize("stat", ["99999999 (python) S 1", "malformed", "", None])
def test_manager_refuses_incompatible_proc_before_creating_state(monkeypatch, tmp_path, stat):
    original = Path.read_text

    def read(path, *args, **kwargs):
        if str(path) == "/proc/self/stat":
            if stat is None:
                raise PermissionError("proc unavailable")
            return stat
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    state = tmp_path / "state"
    with pytest.raises(RuntimeError, match="PID namespace"):
        Manager({"engines": {}}, state)
    assert not state.exists()


@pytest.mark.asyncio
async def test_manager_accepts_actual_proc_namespace(tmp_path):
    assert int(Path("/proc/self/stat").read_text().split()[0]) == os.getpid()
    manager = Manager({"engines": {}}, tmp_path / "state")
    try:
        assert manager.phase == "unloaded"
    finally:
        await manager.close()
