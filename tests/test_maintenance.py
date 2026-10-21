import json
import os
import stat

import pytest

from gateway.config import validate
from gateway.maintenance import Maintenance

NOW = 1_800_000_000.0
OPTIONS = {"dir": None, "holders": ["procurementmind-ci"], "max_ttl_seconds": 5400}


def lease_doc(**overrides):
    doc = {
        "version": 1,
        "id": "0b1c2d3e-4f50-4a6b-8c7d-9e0f1a2b3c4d",
        "holder": "procurementmind-ci",
        "reason": "CI run 123",
        "acquired_at": NOW - 5,
        "expires_at": NOW + 600,
    }
    doc.update(overrides)
    return doc


def make(tmp_path, **options):
    directory = tmp_path / "maint"
    return (
        Maintenance(
            {**OPTIONS, "dir": str(directory), **options}, tmp_path / "state", clock=lambda: NOW
        ),
        directory,
    )


def put(directory, doc, mode=0o600, name="lease.json"):
    path = directory / name
    path.unlink(missing_ok=True)
    path.write_text(doc if isinstance(doc, str) else json.dumps(doc))
    path.chmod(mode)
    return path


def base_config(**maintenance):
    return {
        "engines": {"e": {"models": {}}},
        "maintenance": {"dir": "/tmp/maint", "holders": ["ci"], **maintenance},
    }


def test_valid_lease_is_held(tmp_path):
    m, d = make(tmp_path)
    put(d, lease_doc())
    lease = m.lease()
    assert lease["held"] is True and lease["invalid"] is False and lease["problem"] is None
    assert lease["id"] == lease_doc()["id"] and lease["holder"] == "procurementmind-ci"
    assert lease["reason"] == "CI run 123" and lease["expires_at"] == NOW + 600


def test_absent_and_expired_lease_are_not_held_and_not_invalid(tmp_path):
    m, d = make(tmp_path)
    assert m.lease()["held"] is False and m.lease()["invalid"] is False
    put(d, lease_doc(expires_at=NOW - 1, acquired_at=NOW - 100))
    lease = m.lease()
    assert lease["held"] is False and lease["invalid"] is False and lease["id"] is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"holder": "someone-else"},
        {"expires_at": NOW + 5401},
        {"version": 2},
        {"id": "short"},
        {"id": "has spaces and ../slashes"},
        {"reason": "x" * 201},
        {"reason": "line\nbreak"},
        {"reason": 5},
        {"acquired_at": NOW + 10, "expires_at": NOW + 5},
        {"expires_at": True},
        {"expires_at": "soon"},
        {"extra": 1},
    ],
)
def test_invalid_leases_fail_open_and_are_flagged(tmp_path, overrides):
    m, d = make(tmp_path)
    put(d, lease_doc(**overrides))
    lease = m.lease()
    assert lease["held"] is False and lease["invalid"] is True and lease["problem"]


@pytest.mark.parametrize("text", ["not json", "[]", "null", '{"version":1', ""])
def test_malformed_json_is_invalid_not_held(tmp_path, text):
    m, d = make(tmp_path)
    put(d, text)
    lease = m.lease()
    assert lease["held"] is False and lease["invalid"] is True


def test_missing_key_is_invalid(tmp_path):
    m, d = make(tmp_path)
    doc = lease_doc()
    del doc["reason"]
    put(d, doc)
    assert m.lease()["invalid"] is True


def test_file_permissions_and_size_and_type_are_enforced(tmp_path):
    m, d = make(tmp_path)
    put(d, lease_doc(), mode=0o644)
    assert m.lease()["held"] is False and m.lease()["invalid"] is True
    put(d, lease_doc(), mode=0o400)
    assert m.lease()["held"] is True
    put(d, lease_doc(reason="x" * 200) | {"pad": "p" * 5000})
    assert m.lease()["held"] is False and m.lease()["invalid"] is True
    (d / "lease.json").unlink()
    (d / "lease.json").mkdir()
    assert m.lease()["held"] is False and m.lease()["invalid"] is True


def test_symlinked_lease_is_refused(tmp_path):
    m, d = make(tmp_path)
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps(lease_doc()))
    target.chmod(0o600)
    (d / "lease.json").symlink_to(target)
    lease = m.lease()
    assert lease["held"] is False and lease["invalid"] is True


def test_directory_is_created_private_and_wrong_modes_disable_the_feature(tmp_path):
    m, d = make(tmp_path)
    assert stat.S_IMODE(d.stat().st_mode) == 0o700 and m.error is None
    d.chmod(0o755)
    m2, _ = make(tmp_path)
    assert m2.error and m2.lease()["held"] is False
    assert m2.write_status({"phase": "ready"}) is False
    assert not (d / "status.json").exists()


def test_symlinked_directory_is_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    (tmp_path / "maint").symlink_to(real)
    m, _ = make(tmp_path)
    assert m.error
    assert m.write_status({"phase": "ready"}) is False
    assert not (real / "status.json").exists()


def test_status_file_contents_mode_and_atomic_replace(tmp_path):
    m, d = make(tmp_path)
    put(d, lease_doc())
    assert m.write_status({"phase": "ready", "loaded_model": "m", "active_requests": 2,
                           "native_busy": False, "lifecycle_busy": False}) is True
    path = d / "status.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    status = json.loads(path.read_text())
    assert status["version"] == 1 and status["updated_at"] == NOW
    assert status["phase"] == "ready" and status["loaded_model"] == "m"
    assert status["active_requests"] == 2 and status["lifecycle_busy"] is False
    assert status["lease"]["held"] is True and status["lease"]["id"] == lease_doc()["id"]
    assert status["revoked_ids"] == []
    assert not [p for p in d.iterdir() if p.name.endswith(".tmp")]


def test_status_write_replaces_planted_symlink_without_writing_through(tmp_path):
    m, d = make(tmp_path)
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    (d / "status.json").symlink_to(victim)
    assert m.write_status({"phase": "unloaded"}) is True
    assert victim.read_text() == "keep"
    assert not (d / "status.json").is_symlink()
    assert json.loads((d / "status.json").read_text())["phase"] == "unloaded"


def test_break_records_revocation_and_persists_across_restart(tmp_path):
    m, d = make(tmp_path)
    put(d, lease_doc())
    broken = m.break_lease()
    assert broken["id"] == lease_doc()["id"] and broken["holder"] == "procurementmind-ci"
    assert m.lease()["held"] is False and m.lease()["invalid"] is False
    assert m.revoked_ids() == [lease_doc()["id"]]
    assert (d / "lease.json").exists()  # the watcher's file is never deleted by the gateway
    # A renewal with the same id stays ignored, even after a restart.
    put(d, lease_doc(expires_at=NOW + 900))
    m2, _ = make(tmp_path)
    assert m2.lease()["held"] is False and m2.revoked_ids() == [lease_doc()["id"]]
    # A new id is honoured.
    put(d, lease_doc(id="aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb"))
    assert m2.lease()["held"] is True


def test_break_without_a_lease_returns_none(tmp_path):
    m, d = make(tmp_path)
    assert m.break_lease() is None
    put(d, lease_doc(holder="intruder"))
    assert m.break_lease() is None


def test_revoked_list_is_capped(tmp_path):
    m, d = make(tmp_path)
    for i in range(70):
        put(d, lease_doc(id=f"id-number-{i:04d}"))
        assert m.break_lease() is not None
    ids = m.revoked_ids()
    assert len(ids) == 64 and ids[-1] == "id-number-0069" and "id-number-0000" not in ids


def test_block_detail_formats_message_and_retry_after(tmp_path):
    m, d = make(tmp_path)
    assert m.block_detail() is None
    put(d, lease_doc())
    detail, retry = m.block_detail()
    assert detail == (
        "Model loads are paused by 'procurementmind-ci' until 2027-01-15T08:10:00Z (CI run 123)"
    )
    assert retry == 600
    put(d, lease_doc(expires_at=NOW + 0.2))
    assert m.block_detail()[1] == 1


def test_config_validation_accepts_and_rejects():
    assert validate(base_config())["maintenance"]["holders"] == ["ci"]
    assert validate(base_config(max_ttl_seconds=60))
    assert validate({"engines": {"e": {"models": {}}}})
    for bad in (
        {"dir": "relative/path"},
        {"dir": 5},
        {"holders": []},
        {"holders": ["bad holder"]},
        {"holders": "ci"},
        {"max_ttl_seconds": 59},
        {"max_ttl_seconds": 86401},
        {"max_ttl_seconds": True},
        {"unknown": 1},
    ):
        with pytest.raises(ValueError):
            validate(base_config(**bad))
    with pytest.raises(ValueError):
        validate({"engines": {"e": {"models": {}}}, "maintenance": []})
    with pytest.raises(ValueError):
        validate({"engines": {"e": {"models": {}}}, "maintenance": {"holders": ["ci"]}})


def test_directory_owner_must_be_the_gateway_user(tmp_path, monkeypatch):
    real_getuid = os.getuid
    m, d = make(tmp_path)
    assert m.error is None
    monkeypatch.setattr(os, "getuid", lambda: real_getuid() + 1)
    m2, _ = make(tmp_path)
    assert m2.error
