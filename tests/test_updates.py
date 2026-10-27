"""Transactional engine updates use disposable HTTP engines and local sources."""

import asyncio
import copy
import fcntl
import json
import socket
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

from gateway.app import create_app
from gateway.config import validate
from gateway.updates import Updates, command_env, offline_rollback, private_json, run

SHA = "a" * 40
ROOT = Path(__file__).resolve().parents[1]


def engine_script(root, mode="ok"):
    root.mkdir(parents=True, exist_ok=True)
    script = root / "server"
    source = (ROOT / "tests/fixture_update_backend.py").read_text()
    source = source.replace("#!/usr/bin/env python3", "#!" + sys.executable)
    script.write_text(source)
    script.chmod(0o755)
    (root / "mode").write_text(mode)
    return script


def configuration(tmp_path):
    old = tmp_path / "old"
    script = engine_script(old)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return {
        "allowed_hosts": ["testserver"],
        "startup_timeout": 4,
        "native_matchers": [
            {"argv_contains": str(old)},
            {"argv_contains": str(tmp_path / "state/engine-updates/releases")},
        ],
        "model_roots": {"weights": str(tmp_path / "weights")},
        "engines": {
            "fixture": {
                "models": {
                    "canary": {
                        "argv": [str(script), str(port), "{context}"],
                        "context": 4096,
                        "context_limits": {
                            "min": 1024,
                            "max": 8192,
                            "presets": [4096],
                            "adjustable": True,
                        },
                        "base_url": f"http://127.0.0.1:{port}",
                        "env": {"ROLLBACK_MARKER": "old-environment"},
                    }
                }
            }
        },
        "engine_updates": {
            "fixture": {
                "repository": "example/engine",
                "installed_root": str(old),
                "executable": "server",
                "channel": "tags",
                "build": [[sys.executable, "-c", "print('fixture build')"]],
                "checks": [
                    {"argv": ["{release}/server", "--help"], "contains": ["--port", "--context"]}
                ],
                "tests": [{"model": "canary", "context": 4096, "expected": "IGM_UPDATE_OK"}],
            }
        },
    }


def application(tmp_path):
    cfg = configuration(tmp_path)
    original = copy.deepcopy(cfg)
    app = create_app(validate(cfg), "a" * 32, "b" * 32, tmp_path / "state")
    app.state.original_config = original
    return app


def staged(app, tmp_path, mode="ok"):
    updates = app.state.updates
    folder = updates.root / "releases/fixture/test-release"
    engine_script(folder, mode)
    updates.data["staged"] = {
        "fixture": {SHA: {"root": str(folder), "recipe_hash": updates.recipe_hash("fixture")}}
    }
    updates.data["staged"]["fixture"][SHA]["executable_digest"] = updates.executable_digest(
        folder, updates.recipes["fixture"]
    )
    updates.save()
    return folder


async def cleanup(app):
    await app.state.updates.close()
    await app.state.manager.close()


@pytest.mark.parametrize(
    "change",
    [
        {"repository": "https://arbitrary.invalid/repo"},
        {"installed_root": "/"},
        {"executable": "../../escape"},
        {"channel": "latest-untrusted"},
        {"build": [["relative-command"]]},
        {"checks": []},
        {"tests": []},
        {"tests": [{"model": "canary", "context": 131072, "expected": "OK"}]},
        {"build_timeout": -1},
    ],
)
def test_invalid_admin_recipes_rejected(tmp_path, change):
    cfg = configuration(tmp_path)
    cfg["engine_updates"]["fixture"].update(change)
    with pytest.raises(ValueError):
        validate(cfg)


def test_model_roots_cannot_share_mutable_installation(tmp_path):
    cfg = configuration(tmp_path)
    cfg["model_roots"] = {"weights": str(tmp_path / "old/weights")}
    with pytest.raises(ValueError, match="separate"):
        validate(cfg)


async def test_readonly_check_pins_tags_and_detects_installed_git_version(tmp_path, monkeypatch):
    app = application(tmp_path)
    updater = app.state.updates
    try:

        async def github(path):
            assert path == "example/engine/tags?per_page=10"
            return [{"name": "v2", "commit": {"sha": SHA}}]

        monkeypatch.setattr(updater, "github", github)
        assert (await updater.check("fixture"))["versions"] == [{"name": "v2", "revision": SHA}]
        assert not updater.data["installed"] and app.state.manager.process is None
        assert not (updater.root / "releases").exists()
        with pytest.raises(HTTPException):
            updater.start("stage", "fixture", "b" * 40)
    finally:
        await cleanup(app)


async def test_checks_validate_real_staged_executable(tmp_path):
    app = application(tmp_path)
    updater = app.state.updates
    updater.job = {}
    release = staged(app, tmp_path)
    try:
        await updater.build_release(updater.recipes["fixture"], release)
        updater.recipes["fixture"]["checks"][0]["contains"] = ["nonexistent flag"]
        with pytest.raises(RuntimeError, match="incompatible"):
            await updater.build_release(updater.recipes["fixture"], release)
    finally:
        await cleanup(app)


async def test_apply_authorization_then_verified_rollback_and_restart(tmp_path, monkeypatch):
    app = application(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "private-inherited-token")
    original_base = copy.deepcopy(app.state.updates.base)
    release = staged(app, tmp_path)
    updater = app.state.updates
    m = app.state.manager
    try:
        with pytest.raises(HTTPException, match="Explicit authorization"):
            updater.start("apply", "fixture", SHA)
        updater.start("apply", "fixture", SHA, ["canary"])
        await updater.task
        assert updater.job["phase"] == "complete", updater.job
        assert updater.data["installed"]["fixture"]["revision"] == SHA
        assert m.model is None and m.process is None and not m.native_processes()
        assert str(release) in m.config["engines"]["fixture"]["models"]["canary"]["argv"][0]
        receipt = updater.data["history"][-1]
        assert receipt["tests"][0]["stream"] and receipt["tests"][0]["exit"]
        assert "private-inherited-token" not in updater.path.read_text()
        # A restarted gateway applies static version bindings before registry replay.
        cfg = copy.deepcopy(app.state.original_config)
        again = Updates(cfg, tmp_path / "state")
        assert cfg["engines"]["fixture"]["models"]["canary"]["argv"][0].startswith(str(release))
        await again.close()
        monkeypatch.setenv("ROLLBACK_MARKER", "changed-since-update")
        updater.start("rollback", "fixture", test_models=["canary"])
        await updater.task
        assert updater.job["phase"] == "complete", updater.job
        restored = m.config["engines"]["fixture"]["models"]["canary"]
        assert restored["argv"] == original_base["fixture"]["models"]["canary"]["argv"]
        assert restored["env"]["ROLLBACK_MARKER"] == "old-environment"
        assert not m.update_blocked and m.process is None
        assert (release / "server").exists(), "Rollback must not delete retained versions"
    finally:
        await cleanup(app)


@pytest.mark.parametrize(
    "mode,step", [("bad_answer", "testing_answer"), ("bad_stream", "testing_stream")]
)
async def test_http_200_is_not_acceptance_and_failed_update_restores_old_version(
    tmp_path, mode, step
):
    app = application(tmp_path)
    staged(app, tmp_path, mode)
    updater = app.state.updates
    try:
        updater.start("apply", "fixture", SHA, ["canary"])
        await updater.task
        assert updater.job["phase"] == "failed"
        record = updater.data["history"][-1]
        assert record["failed_step"] == step and record["rollback"] == "verified"
        assert not updater.data["recovery_required"] and not app.state.manager.update_blocked
        assert (
            str(tmp_path / "old")
            in app.state.manager.config["engines"]["fixture"]["models"]["canary"]["argv"][0]
        )
    finally:
        await cleanup(app)


async def test_failed_restore_blocks_inference_until_explicit_recovery(tmp_path):
    app = application(tmp_path)
    staged(app, tmp_path, "bad_answer")
    updater, m = app.state.updates, app.state.manager
    (tmp_path / "old/mode").write_text("bad_answer")
    try:
        updater.start("apply", "fixture", SHA, ["canary"])
        await updater.task
        assert updater.data["recovery_required"] and m.update_blocked
        assert updater.data["recovery_engine"] == "fixture"
        with pytest.raises(HTTPException) as exc:
            await m.load_model("canary", engine="fixture")
        assert exc.value.status_code == 423
        (tmp_path / "old/mode").write_text("ok")
        updater.start("rollback", "fixture", test_models=["canary"])
        await updater.task
        assert updater.job["phase"] == "complete", updater.job
        assert not m.update_blocked and not updater.data["recovery_required"]
    finally:
        await cleanup(app)


async def test_busy_engine_and_external_lease_refuse_transition(tmp_path):
    app = application(tmp_path)
    updater, m = app.state.updates, app.state.manager
    staged(app, tmp_path)
    try:
        m.active["busy"] = {"task": None}
        updater.start("apply", "fixture", SHA, ["canary"])
        await updater.task
        assert updater.job["phase"] == "failed" and not updater.data["installed"]
        m.active.clear()
        m.lease_guard = lambda: ("External memory job", 30)
        updater.start("apply", "fixture", SHA, ["canary"])
        await updater.task
        assert updater.job["phase"] == "failed" and m.process is None
    finally:
        await cleanup(app)


async def test_concurrent_api_operations_cannot_enter_transaction(tmp_path):
    app = application(tmp_path)
    staged(app, tmp_path, "slow")
    updater, m = app.state.updates, app.state.manager
    try:
        updater.start("apply", "fixture", SHA, ["canary"])
        for _ in range(100):
            if m.update_blocked:
                break
            await asyncio.sleep(0.01)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": "Bearer " + "a" * 32},
        ) as client:
            for url, body in [
                ("/api/engine", {"engine": "fixture", "force": True}),
                ("/api/model", {"model": "canary", "force": True}),
                ("/api/unload", {"force": True}),
                ("/v1/chat/completions", {"model": "canary"}),
                ("/api/models", {}),
            ]:
                response = await client.post(url, json=body)
                assert response.status_code in (409, 423), (url, response.text)
        await updater.task
        assert updater.job["phase"] == "complete", updater.job
    finally:
        await cleanup(app)


async def test_interrupted_transaction_restores_durable_pointer_without_model_load(tmp_path):
    app = application(tmp_path)
    updater = app.state.updates
    staged(app, tmp_path)
    try:
        updater.start("apply", "fixture", SHA, ["canary"])
        await updater.task
        before = copy.deepcopy(updater.data["installed"])
        updater.data["pending"] = {"before": before, "engine": "fixture"}
        updater.save()
        cfg = copy.deepcopy(app.state.original_config)
        recovery = Updates(cfg, tmp_path / "state")
        assert recovery.data["recovery_required"] and recovery.data["pending"] is None
        assert recovery.data["installed"] == before and "default" not in cfg
        await recovery.close()
    finally:
        await cleanup(app)


async def test_offline_rollback_refuses_running_supervisor_and_restores_pointer(tmp_path):
    app = application(tmp_path)
    updater = app.state.updates
    staged(app, tmp_path)
    try:
        updater.start("apply", "fixture", SHA, ["canary"])
        await updater.task
        lock_path = tmp_path / "state/manager.lock"
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with pytest.raises(ValueError, match="Stop the gateway"):
                offline_rollback(tmp_path / "state", "fixture")
        offline_rollback(tmp_path / "state", "fixture")
        data = json.loads(updater.path.read_text())
        assert data["recovery_required"] and data["installed"]["fixture"]["revision"] is None
    finally:
        await cleanup(app)


async def test_report_allowlist_excludes_secrets_paths_models_logs_and_outputs(tmp_path):
    app = application(tmp_path)
    updater = app.state.updates
    try:
        updater.data["history"] = [
            {
                "engine": "fixture",
                "action": "apply",
                "revision": SHA,
                "result": "failed",
                "rollback": "verified",
                "tests": [{"model": "PRIVATE_MODEL"}],
                "raw_log": "TOKEN=secret",
                "before_config": {"env": {"SECRET": "sensitive"}},
                "path": "/home/private/person",
                "output": "private generated answer",
            }
        ]
        report = updater.report()
        text = json.dumps(report)
        for secret in (
            "PRIVATE_MODEL",
            "TOKEN=",
            "sensitive",
            "/home/private",
            "private generated answer",
        ):
            assert secret not in text
        assert report["issue_url"].startswith(
            "https://github.com/vkotios/inference-gateway-manager/issues/new?"
        )
        assert "issues are public" in report["body"]
    finally:
        await cleanup(app)


async def test_api_requires_control_key_and_rejects_client_commands(tmp_path):
    app = application(tmp_path)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as c:
            assert (
                await c.get("/api/updates", headers={"Authorization": "Bearer " + "b" * 32})
            ).status_code == 403
            r = await c.post(
                "/api/updates/fixture",
                headers={"Authorization": "Bearer " + "a" * 32},
                json={"action": "stage", "revision": SHA, "argv": ["rm"]},
            )
            assert r.status_code == 422
    finally:
        await cleanup(app)


async def test_command_timeout_kills_owned_children_and_scrubs_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("IGM_CONTROL_KEY", "private")
    monkeypatch.setenv("GITHUB_TOKEN", "private")
    assert "IGM_CONTROL_KEY" not in command_env() and "GITHUB_TOKEN" not in command_env()
    with pytest.raises(TimeoutError):
        await run([sys.executable, "-c", "import time; time.sleep(10)"], tmp_path, 0.1)


def test_private_journal_uses_owner_only_permissions(tmp_path):
    path = tmp_path / "state.json"
    private_json(path, {"state": "private"})
    assert path.stat().st_mode & 0o777 == 0o600


async def test_modified_stage_is_rejected_before_stopping_current_engine(tmp_path):
    app = application(tmp_path)
    updater = app.state.updates
    folder = staged(app, tmp_path)
    (folder / "server").write_text("#!/bin/sh\necho changed\n")
    try:
        updater.start("apply", "fixture", SHA, ["canary"])
        await updater.task
        assert updater.job["phase"] == "failed"
        assert not updater.data["installed"] and updater.data["pending"] is None
        assert not app.state.manager.update_blocked
        assert "failed" in updater.report()["body"]
    finally:
        await cleanup(app)


async def test_local_git_stage_uses_pinned_commit_and_keeps_old_install(tmp_path, monkeypatch):
    import subprocess

    import gateway.updates as module

    app = application(tmp_path)
    updater = app.state.updates
    source = tmp_path / "source-repo"
    engine_script(source)

    def git(*args):
        return subprocess.check_output(["git", "-C", str(source), *args], text=True).strip()

    git("init", "-q")
    git("add", "server", "mode")
    git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "fixture source",
    )
    sha = git("rev-parse", "HEAD")
    real_run = module.run

    async def local_source(argv, *args, **kwargs):
        if argv[-3:-1] == ["add", "origin"]:
            argv = [*argv[:-1], str(source)]
        return await real_run(argv, *args, **kwargs)

    monkeypatch.setattr(module, "run", local_source)
    updater.available["fixture"] = [{"name": "fixture", "revision": sha}]
    before = (tmp_path / "old/server").read_bytes()
    try:
        updater.start("stage", "fixture", sha)
        await updater.task
        assert updater.job["phase"] == "complete", updater.job
        stage = updater.data["staged"]["fixture"][sha]
        assert stage["executable_digest"]
        assert Path(stage["root"]).is_relative_to(updater.root / "releases")
        assert (tmp_path / "old/server").read_bytes() == before
        assert app.state.manager.process is None
    finally:
        await cleanup(app)


async def test_changed_administrator_config_is_not_silently_overwritten(tmp_path):
    app = application(tmp_path)
    updater = app.state.updates
    staged(app, tmp_path)
    try:
        updater.start("apply", "fixture", SHA, ["canary"])
        await updater.task
        cfg = copy.deepcopy(app.state.original_config)
        cfg["engines"]["fixture"]["models"]["canary"]["context"] = 8192
        with pytest.raises(ValueError, match="configuration changed"):
            Updates(cfg, tmp_path / "state")
    finally:
        await cleanup(app)


async def test_shutdown_during_candidate_test_leaves_verification_required(tmp_path):
    app = application(tmp_path)
    staged(app, tmp_path, "slow")
    updater = app.state.updates
    try:
        updater.start("apply", "fixture", SHA, ["canary"])
        for _ in range(200):
            if updater.job["phase"] in {"testing_answer", "testing_stream"}:
                break
            await asyncio.sleep(0.01)
        updater.task.cancel()
        await asyncio.gather(updater.task, return_exceptions=True)
        assert updater.data["pending"] is None
        assert updater.data["recovery_required"] and app.state.manager.update_blocked
        assert app.state.manager.process is None
    finally:
        await cleanup(app)


async def test_registered_model_profiles_rebind_on_restart_without_static_collision(tmp_path):
    import struct

    from gateway.registry import Registry

    cfg = configuration(tmp_path)
    profile = copy.deepcopy(cfg["engines"]["fixture"]["models"]["canary"])
    profile.update(format="gguf")
    profile["argv"].append("{path}")
    cfg["engines"]["fixture"]["profiles"] = {"gguf": profile}
    original = copy.deepcopy(cfg)
    (tmp_path / "weights").mkdir()
    model = tmp_path / "weights/test.gguf"
    model.write_bytes(b"GGUF" + struct.pack("<IQQ", 3, 0, 0))
    app = create_app(validate(cfg), "a" * 32, "b" * 32, tmp_path / "state")
    updater = app.state.updates
    try:
        app.state.registry.register(
            {"engine": "fixture", "profile": "gguf", "model": "registered", "path": str(model)}
        )
        release = staged(app, tmp_path)
        updater.start("apply", "fixture", SHA, ["canary"])
        await updater.task
        assert updater.job["phase"] == "complete", updater.job
        restored = Updates(original, tmp_path / "state")
        try:
            registry = Registry(original, tmp_path / "state")
            assert not registry.unavailable
            argv = original["engines"]["fixture"]["models"]["registered"]["argv"]
            assert argv[0] == str(release / "server") and str(model) in argv
        finally:
            await restored.close()
    finally:
        await cleanup(app)


async def test_native_unknown_occupancy_preserves_running_old_engine(tmp_path, monkeypatch):
    app = application(tmp_path)
    staged(app, tmp_path)
    updater, m = app.state.updates, app.state.manager
    try:
        await m.load_model("canary", engine="fixture")
        process = m.process

        async def unknown():
            return None

        monkeypatch.setattr(m, "native_busy", unknown)
        updater.start("apply", "fixture", SHA, ["canary"])
        await updater.task
        assert updater.job["phase"] == "failed" and process.poll() is None
        assert not updater.data["installed"] and not m.update_blocked
    finally:
        await cleanup(app)


async def test_packaged_install_can_report_version_without_git_metadata(tmp_path, monkeypatch):
    app = application(tmp_path)
    updater = app.state.updates
    updater.recipes["fixture"]["version_argv"] = [
        sys.executable,
        "-c",
        "print('Fixture engine v1.2.3')",
    ]

    async def github(path):
        return [{"name": "v2", "commit": {"sha": SHA}}]

    monkeypatch.setattr(updater, "github", github)
    try:
        result = await updater.check("fixture")
        assert result["installed_version"] == "1.2.3"
        assert updater.view()["engines"][0]["installed_version"] == "1.2.3"
        assert app.state.manager.process is None
    finally:
        await cleanup(app)
