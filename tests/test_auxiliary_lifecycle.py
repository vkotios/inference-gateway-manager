"""Chat priority, footprint-driven fit, locking, drain, restore policy and calibration."""

import asyncio
import io
import json
import socket
import sys
import wave
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

from gateway.app import create_app
from gateway.footprints import Footprint, aux_fingerprint, chat_fingerprint

GIB = 1024**3
BACKEND = str(Path(__file__).with_name("fixture_backend.py").resolve())
WORKER = str(Path(__file__).with_name("fixture_aux_worker.py").resolve())
ADMIN = {"Authorization": "Bearer " + "a" * 32}
INFER = {"Authorization": "Bearer " + "b" * 32}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def chat_spec():
    port = free_port()
    return {
        "argv": [sys.executable, BACKEND, str(port)],
        "base_url": f"http://127.0.0.1:{port}",
        "upstream_model": "fixture",
    }


class Env:
    """An app with two fixture chat models and four auxiliary models."""

    def __init__(
        self, tmp_path, monkeypatch, aux_options=None, footprints=None, total=130 * GIB, config=None
    ):
        self.tmp_path = tmp_path
        self.gate = None
        self.entered = asyncio.Event()
        self.fail_spawn = False
        # Model directories are stable and separate from the state directory, whose contents
        # change constantly while the gateway runs.
        models = tmp_path / "models"
        specs = {}
        for name in ("mini", "mini2", "whisper", "big-asr"):
            (models / name).mkdir(parents=True, exist_ok=True)
            (models / name / "weights.bin").write_bytes(b"x" * 1024)
            specs[name] = {"path": str(models / name), "threads": 1}
        self.config = config or {
            "allowed_hosts": ["testserver"],
            "engines": {"one": {"models": {"small": chat_spec(), "big": chat_spec()}}},
            "auxiliary": {
                "minimum_free_bytes": 10,
                "host_baseline_bytes": 6 * GIB,
                "embeddings": {"models": {"mini": specs["mini"], "mini2": specs["mini2"]}},
                "transcription": {
                    "models": {"whisper": specs["whisper"], "big-asr": specs["big-asr"]}
                },
                **(aux_options or {}),
            },
            "footprints": {"interval_seconds": 0.05, "settle_seconds": 0, **(footprints or {})},
        }
        self.app = create_app(self.config, "a" * 32, "b" * 32, tmp_path)
        self.manager = self.app.state.manager
        self.aux = self.app.state.auxiliary
        self.store = self.aux.store
        self.aux.memory._sample = lambda: total
        self.aux.sample_total = lambda: total
        native = asyncio.create_subprocess_exec
        env = self

        async def fixture_process(*args, **kwargs):
            if len(args) > 2 and args[1].endswith("/gateway/aux_worker.py"):
                env.entered.set()
                if env.gate is not None:
                    await env.gate.wait()
                if env.fail_spawn:
                    raise OSError("spawn failed")
                args = (sys.executable, WORKER, args[2])
            return await native(*args, **kwargs)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fixture_process)

    def client(self):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://testserver"
        )

    def record_chat(self, model, gib, context=None):
        _engine, spec, settings = self.manager.prepare_model(model, "one")
        self.store.record(
            Footprint(
                "chat",
                f"one/{model}",
                int(gib * GIB),
                int(gib * GIB),
                0,
                context,
                chat_fingerprint("one", model, spec),
                "measured",
                0.0,
                "test",
            )
        )

    def record_aux(self, name, model, steady_gib, request_gib):
        spec = self.aux.get(name).models[model]
        steady, request = int(steady_gib * GIB), int(request_gib * GIB)
        self.store.record(
            Footprint(
                name,
                model,
                steady,
                steady + request,
                request,
                None,
                aux_fingerprint(name, model, spec),
                "measured",
                0.0,
                "test",
                1,
            )
        )

    async def load_aux(self, name, model):
        await self.aux.enable(name, True)
        await self.aux.load(name, model)

    async def close(self):
        await self.aux.close()
        await self.manager.close()


@pytest.fixture
async def env(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch)
    await e.manager.select_engine("one")
    yield e
    await e.close()


def loaded(e, name):
    return e.aux.services[name].process is not None


# ---- chat priority ---------------------------------------------------------------------


async def test_chat_load_keeps_services_that_fit(env):
    env.record_chat("small", 40)
    env.record_aux("embeddings", "mini", 1, 1)
    env.record_aux("transcription", "whisper", 6, 6)
    await env.load_aux("embeddings", "mini")
    await env.load_aux("transcription", "whisper")
    await env.manager.load_model("small")
    assert env.manager.phase == "ready"
    assert loaded(env, "embeddings") and loaded(env, "transcription")
    assert env.aux.services["embeddings"].suspended_reason is None


async def test_chat_load_unloads_only_what_does_not_fit_and_keeps_desired_state(env):
    env.record_chat("big", 112)
    env.record_aux("embeddings", "mini", 1, 1)  # needs 2 GiB
    env.record_aux("transcription", "whisper", 6, 6)  # needs 12 GiB: 112 + 2 + 12 > 124
    await env.load_aux("embeddings", "mini")
    await env.load_aux("transcription", "whisper")
    await env.manager.load_model("big")
    assert env.manager.phase == "ready"
    assert loaded(env, "embeddings") and not loaded(env, "transcription")
    asr = env.aux.services["transcription"]
    assert asr.suspended_reason == "chat_priority" and asr.desired_model == "whisper"
    assert asr.enabled is True  # unloaded for chat, not disabled
    view = env.aux.view()["transcription"]
    assert view["suspended_reason"] == "chat_priority" and view["resume_in_seconds"] > 0


async def test_uncalibrated_chat_model_unloads_every_service(env):
    await_env = env
    await_env.record_aux("embeddings", "mini", 1, 1)
    await await_env.load_aux("embeddings", "mini")
    await env.manager.load_model("big")  # no footprint recorded for it
    assert not loaded(env, "embeddings")
    assert env.aux.services["embeddings"].suspended_reason == "chat_uncalibrated"
    assert env.manager.phase == "ready"  # the chat model is never refused because of services


async def test_idempotent_chat_load_does_not_touch_services(env):
    env.record_chat("small", 40)
    env.record_aux("embeddings", "mini", 1, 1)
    await env.load_aux("embeddings", "mini")
    await env.manager.load_model("small")
    pid = env.aux.services["embeddings"].process.pid
    await env.manager.load_model("small")  # same model, same settings: a no-op
    assert loaded(env, "embeddings") and env.aux.services["embeddings"].process.pid == pid


async def test_requests_get_retryable_503_while_unloaded_for_chat(env):
    env.record_aux("embeddings", "mini", 1, 1)
    await env.load_aux("embeddings", "mini")
    await env.manager.load_model("big")  # uncalibrated: services unloaded
    async with env.client() as c:
        r = await c.post("/v1/embeddings", json={"model": "mini", "input": "x"}, headers=INFER)
    assert r.status_code == 503
    assert r.headers["x-igm-code"] == "auxiliary_unloaded_for_chat"
    assert int(r.headers["retry-after"]) > 0


# ---- admission of services next to a chat model -----------------------------------------


async def test_service_cannot_load_next_to_uncalibrated_chat_model(env):
    await env.manager.load_model("small")  # nothing recorded
    await env.aux.enable("embeddings", True)
    with pytest.raises(HTTPException) as exc:
        await env.aux.load("embeddings", "mini")
    assert exc.value.status_code == 409 and exc.value.headers["X-IGM-Code"] == "model_uncalibrated"
    assert not loaded(env, "embeddings")


async def test_service_loads_when_combination_fits_and_is_refused_when_not(env):
    env.record_chat("big", 110)
    env.record_aux("transcription", "whisper", 2, 2)  # needs 4: 114 <= 124
    env.record_aux("transcription", "big-asr", 10, 6)  # needs 16: 126 > 124
    await env.manager.load_model("big")
    await env.aux.enable("transcription", True)
    await env.aux.load("transcription", "whisper")  # fits
    await env.aux.unload("transcription")
    with pytest.raises(HTTPException) as exc:
        await env.aux.load("transcription", "big-asr")
    assert exc.value.status_code == 503
    assert exc.value.headers["X-IGM-Code"] == "insufficient_memory"
    assert "needs" in exc.value.detail and "budget" in exc.value.detail
    assert env.aux.memory.reserved_bytes == 0


async def test_fit_is_model_agnostic_a_larger_model_changes_which_chat_models_fit(env):
    """Same chat model: fits with one transcription model, evicts a larger one."""
    env.record_chat("big", 112)
    env.record_aux("transcription", "whisper", 2, 2)
    env.record_aux("transcription", "big-asr", 10, 6)
    await env.load_aux("transcription", "whisper")
    await env.manager.load_model("big")
    assert loaded(env, "transcription")  # 112 + 4 fits
    await env.manager.unload()
    await env.aux.unload("transcription")
    await env.load_aux("transcription", "big-asr")
    await env.manager.load_model("big")
    assert not loaded(env, "transcription")  # 112 + 16 does not
    suspended = env.aux.services["transcription"]
    assert suspended.suspended_reason == "chat_priority" and suspended.desired_model == "big-asr"


async def test_unknown_total_memory_fails_closed(env):
    env.record_aux("embeddings", "mini", 1, 1)
    env.aux.sample_total = lambda: (_ for _ in ()).throw(OSError("no meminfo"))
    await env.aux.enable("embeddings", True)
    with pytest.raises(HTTPException) as exc:
        await env.aux.load("embeddings", "mini")
    assert exc.value.status_code == 503 and exc.value.headers["X-IGM-Code"] == "memory_unknown"


# ---- locking and priority -------------------------------------------------------------


async def test_service_load_does_not_hold_the_lifecycle_lock(env):
    env.record_aux("embeddings", "mini", 1, 1)
    env.gate = asyncio.Event()
    await env.aux.enable("embeddings", True)
    task = asyncio.create_task(env.aux.load("embeddings", "mini"))
    await asyncio.wait_for(env.entered.wait(), 5)
    assert not env.manager.lock.locked() and not env.manager.lifecycle_busy
    env.gate.set()
    await task
    assert loaded(env, "embeddings")


async def test_chat_load_cancels_an_inflight_service_load(env):
    env.record_chat("big", 123)
    env.record_aux("embeddings", "mini", 1, 1)  # 123 + 2 > 124: cannot coexist
    env.gate = asyncio.Event()  # never opened: the worker start is stuck
    await env.aux.enable("embeddings", True)
    task = asyncio.create_task(env.aux.load("embeddings", "mini"))
    await asyncio.wait_for(env.entered.wait(), 5)
    await asyncio.wait_for(env.manager.load_model("big"), 20)
    assert env.manager.phase == "ready"
    with pytest.raises(HTTPException) as exc:
        await asyncio.wait_for(task, 5)
    assert exc.value.status_code == 409 and exc.value.headers["X-IGM-Code"] == "chat_priority"
    assert not loaded(env, "embeddings") and env.aux.memory.reserved_bytes == 0


async def test_racing_chat_and_service_loads_do_not_deadlock(env):
    env.record_chat("small", 30)
    env.record_aux("embeddings", "mini", 1, 1)
    env.record_aux("transcription", "whisper", 2, 2)
    await env.aux.enable("embeddings", True)
    await env.aux.enable("transcription", True)
    results = await asyncio.wait_for(
        asyncio.gather(
            env.manager.load_model("small"),
            env.aux.load("embeddings", "mini"),
            env.aux.load("transcription", "whisper"),
            return_exceptions=True,
        ),
        30,
    )
    for item in results:
        assert not isinstance(item, BaseException) or isinstance(item, HTTPException)


# ---- drain ----------------------------------------------------------------------------


async def test_inflight_request_finishes_during_drain_then_service_stops(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch, aux_options={"drain_seconds": {"embeddings": 10}})
    await e.manager.select_engine("one")
    try:
        e.record_aux("embeddings", "mini", 1, 1)
        await e.load_aux("embeddings", "mini")
        pending = asyncio.create_task(e.aux.infer("embeddings", "mini", {"input": ["wait"]}))
        while not e.aux.services["embeddings"].busy:
            await asyncio.sleep(0)
        await e.manager.load_model("big")  # uncalibrated: must unload the service, after draining
        assert (await pending)["vectors"] == [[0.1, 0.2]]
        assert not loaded(e, "embeddings")
    finally:
        await e.close()


async def test_drain_timeout_stops_the_worker_and_reports_unloaded(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch, aux_options={"drain_seconds": 0})
    await e.manager.select_engine("one")
    try:
        e.record_aux("embeddings", "mini", 1, 1)
        await e.load_aux("embeddings", "mini")
        pending = asyncio.create_task(e.aux.infer("embeddings", "mini", {"input": ["wait"]}))
        while not e.aux.services["embeddings"].busy:
            await asyncio.sleep(0)
        await e.manager.load_model("big")
        with pytest.raises(HTTPException) as exc:
            await pending
        assert exc.value.status_code == 503
        assert exc.value.headers["X-IGM-Code"] == "auxiliary_unloaded_for_chat"
    finally:
        await e.close()


# ---- reservations ---------------------------------------------------------------------


async def test_transcription_reservations_are_sequential_not_nested(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch, total=3 * GIB)
    e.config["auxiliary"]["host_baseline_bytes"] = 1  # isolate the live-admission check
    e.aux.options["host_baseline_bytes"] = 1
    try:
        e.record_aux("transcription", "whisper", 1, 1)  # request estimate 1 GiB, load peak 2 GiB
        await e.load_aux("transcription", "whisper")
        audio = io.BytesIO()
        with wave.open(audio, "wb") as wav:
            wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            wav.writeframes(b"\0\0" * 16000)
        # decode needs ~2.1 GiB, the worker 1 GiB: 3.1 GiB if counted together (too much for
        # 3 GiB), 2.1 GiB when the decode reservation is released first.
        async with e.client() as c:
            r = await c.post(
                "/v1/audio/transcriptions",
                headers=INFER,
                data={"model": "whisper"},
                files={"file": ("a.wav", audio.getvalue(), "audio/wav")},
            )
        assert r.status_code == 200, r.text
        assert e.aux.memory.reserved_bytes == 0
    finally:
        await e.close()


# ---- recorder and API ----------------------------------------------------------------


async def wait_for(predicate, seconds=5):
    for _ in range(int(seconds / 0.05)):
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return False


async def test_loaded_chat_model_is_recorded_and_visible_over_the_api(env):
    await env.manager.load_model("small")
    _e, spec, _s = env.manager.prepare_model("small", "one")
    fingerprint = chat_fingerprint("one", "small", spec)
    assert await wait_for(lambda: env.store.lookup("chat", "one/small", fingerprint) is not None)
    record = env.store.lookup("chat", "one/small", fingerprint)
    assert record.source == "measured" and record.steady_bytes > 0
    async with env.client() as c:
        view = (await c.get("/api/footprints", headers=ADMIN)).json()
        assert any(r["key"] == "one/small" and r["source"] == "measured" for r in view["records"])
        entry = next(x for x in view["compatibility"] if x["model"] == "small")
        assert entry["calibrated"] is True
        assert next(x for x in view["compatibility"] if x["model"] == "big")["calibrated"] is False
        assert view["memory"]["reserve_bytes"] == 10
        assert (await c.post("/api/footprints/record", headers=ADMIN)).status_code == 200
        assert (await c.get("/api/footprints", headers=INFER)).status_code == 403
        forgotten = await c.post(
            "/api/footprints/forget", json={"kind": "chat", "key": "one/small"}, headers=ADMIN
        )
        assert forgotten.json()["removed"] >= 1


async def test_record_now_requires_a_loaded_chat_model(env):
    async with env.client() as c:
        assert (await c.post("/api/footprints/record", headers=ADMIN)).status_code == 409


async def test_calibrate_reloads_records_and_aborts_when_memory_collapses(env, monkeypatch):
    async with env.client() as c:
        r = await c.post(
            "/api/footprints/calibrate", json={"engine": "one", "model": "small"}, headers=ADMIN
        )
        assert r.status_code == 200, r.text
        assert r.json()["record"]["key"] == "one/small" and r.json()["record"]["steady_bytes"] > 0
        assert (
            await c.post(
                "/api/footprints/calibrate", json={"engine": "one", "model": "nope"}, headers=ADMIN
            )
        ).status_code == 404
        await env.manager.unload()
        monkeypatch.setattr("gateway.app.effective_available_memory", lambda: 0)
        r = await c.post(
            "/api/footprints/calibrate", json={"engine": "one", "model": "big"}, headers=ADMIN
        )
        assert r.status_code == 503 and "aborted" in r.json()["detail"]
        assert env.manager.phase != "ready"


# ---- persistence and restore ------------------------------------------------------------


def restore_env(tmp_path, monkeypatch, policy, **extra):
    return Env(tmp_path, monkeypatch, aux_options={"restore": policy, **extra})


async def test_clean_shutdown_restores_desired_services_after_restart(tmp_path, monkeypatch):
    first = restore_env(tmp_path, monkeypatch, "after_clean_shutdown")
    first.record_aux("embeddings", "mini", 1, 1)
    await first.load_aux("embeddings", "mini")
    config = first.config
    await first.close()
    state = json.loads((tmp_path / "auxiliary-state.json").read_text())
    assert (
        state["clean_shutdown"] is True
        and state["services"]["embeddings"]["desired_model"] == "mini"
    )
    second = Env(tmp_path, monkeypatch, config=config)
    try:
        service = second.aux.services["embeddings"]
        assert (
            service.enabled and service.desired_model == "mini" and not loaded(second, "embeddings")
        )
        await second.aux.reconcile_once()
        assert loaded(second, "embeddings")
    finally:
        await second.close()


async def test_policy_never_restores_nothing(tmp_path, monkeypatch):
    first = restore_env(tmp_path, monkeypatch, "never")
    first.record_aux("embeddings", "mini", 1, 1)
    await first.load_aux("embeddings", "mini")
    config = first.config
    await first.close()
    second = Env(tmp_path, monkeypatch, config=config)
    try:
        service = second.aux.services["embeddings"]
        assert not service.enabled and service.desired_model is None
        await second.aux.reconcile_once()
        assert not loaded(second, "embeddings")
    finally:
        await second.close()


async def test_unclean_shutdown_blocks_restore_and_remembers_what_was_wanted(tmp_path, monkeypatch):
    first = restore_env(tmp_path, monkeypatch, "after_clean_shutdown")
    first.record_aux("embeddings", "mini", 1, 1)
    await first.load_aux("embeddings", "mini")
    second = Env(tmp_path, monkeypatch, config=first.config)  # first never shut down cleanly
    try:
        assert "not clean" in second.aux.blocked_reason
        service = second.aux.services["embeddings"]
        assert not service.enabled and service.previous["desired_model"] == "mini"
        await second.aux.reconcile_once()
        assert not loaded(second, "embeddings")
    finally:
        await second.close()
        await first.close()


async def test_incomplete_automatic_restore_blocks_the_next_start(tmp_path, monkeypatch):
    first = restore_env(tmp_path, monkeypatch, "after_clean_shutdown")
    config = first.config
    await first.close()
    state_path = tmp_path / "auxiliary-state.json"
    state = json.loads(state_path.read_text())
    state.update(
        clean_shutdown=True, restore_in_progress={"service": "embeddings", "model": "mini", "at": 1}
    )
    state["services"]["embeddings"] = {"enabled": True, "desired_model": "mini"}
    state_path.write_text(json.dumps(state))
    second = Env(tmp_path, monkeypatch, config=config)
    try:
        assert "did not complete" in second.aux.blocked_reason
        assert not second.aux.services["embeddings"].enabled
    finally:
        await second.close()


async def test_restore_marker_is_written_during_and_cleared_after_an_attempt(tmp_path, monkeypatch):
    e = restore_env(tmp_path, monkeypatch, "after_clean_shutdown")
    try:
        e.record_aux("embeddings", "mini", 1, 1)
        service = e.aux.services["embeddings"]
        service.enabled, service.desired_model = True, "mini"
        e.gate = asyncio.Event()
        task = asyncio.create_task(e.aux.reconcile_once())
        await asyncio.wait_for(e.entered.wait(), 5)
        assert (
            json.loads((tmp_path / "auxiliary-state.json").read_text())["restore_in_progress"][
                "service"
            ]
            == "embeddings"
        )
        e.gate.set()
        await task
        assert (
            json.loads((tmp_path / "auxiliary-state.json").read_text())["restore_in_progress"]
            is None
        )
        assert loaded(e, "embeddings")
    finally:
        await e.close()


async def test_explicit_unload_clears_desired_state(tmp_path, monkeypatch):
    first = restore_env(tmp_path, monkeypatch, "after_clean_shutdown")
    first.record_aux("embeddings", "mini", 1, 1)
    await first.load_aux("embeddings", "mini")
    await first.aux.unload("embeddings")
    config = first.config
    await first.close()
    second = Env(tmp_path, monkeypatch, config=config)
    try:
        await second.aux.reconcile_once()
        assert not loaded(second, "embeddings")
    finally:
        await second.close()


async def test_holdoff_delays_restore_after_chat_priority_and_memory_decides(tmp_path, monkeypatch):
    e = restore_env(tmp_path, monkeypatch, "after_clean_shutdown", restore_holdoff_seconds=100)
    await e.manager.select_engine("one")
    clock = {"v": 0.0}
    e.aux.clock = lambda: clock["v"]
    try:
        e.record_aux("embeddings", "mini", 1, 1)
        await e.load_aux("embeddings", "mini")
        await e.manager.load_model("big")  # uncalibrated: unloaded for chat
        assert e.aux.services["embeddings"].suspended_reason == "chat_uncalibrated"
        clock["v"] = 50
        await e.aux.reconcile_once()
        assert not loaded(e, "embeddings")  # hold-off not over
        clock["v"] = 101
        await e.aux.reconcile_once()
        assert not loaded(e, "embeddings")  # chat model still uncalibrated: refused, not a failure
        assert e.aux.attempts == []
        e.record_chat("big", 40)
        await e.aux.reconcile_once()
        assert loaded(e, "embeddings") and e.aux.services["embeddings"].suspended_reason is None
    finally:
        await e.close()


async def test_repeated_failed_restores_suspend_automatic_restore(tmp_path, monkeypatch):
    e = restore_env(tmp_path, monkeypatch, "after_clean_shutdown")
    try:
        e.record_aux("embeddings", "mini", 1, 1)
        service = e.aux.services["embeddings"]
        service.enabled, service.desired_model = True, "mini"
        e.fail_spawn = True
        for _ in range(3):
            await e.aux.reconcile_once()
        assert len(e.aux.attempts) == 3
        await e.aux.reconcile_once()
        assert "repeated failures" in e.aux.blocked_reason
        e.fail_spawn = False
        await e.aux.reconcile_once()
        assert not loaded(e, "embeddings")  # stays suspended until an operator acts
        await e.aux.load("embeddings", "mini")  # explicit action clears the block
        assert loaded(e, "embeddings") and e.aux.blocked_reason is None
    finally:
        await e.close()


async def test_on_demand_policy_restores_on_the_first_request(tmp_path, monkeypatch):
    e = restore_env(tmp_path, monkeypatch, "on_demand")
    try:
        e.record_aux("embeddings", "mini", 1, 1)
        service = e.aux.services["embeddings"]
        service.enabled, service.desired_model = True, "mini"
        await e.aux.reconcile_once()
        assert not loaded(e, "embeddings")  # nothing happens without demand
        with pytest.raises(HTTPException) as exc:
            await e.aux.infer("embeddings", "mini", {"input": ["x"]})
        assert (
            exc.value.status_code == 503
            and exc.value.headers["X-IGM-Code"] == "auxiliary_restoring"
        )
        await e.aux.reconcile_once()
        assert loaded(e, "embeddings")
        assert (await e.aux.infer("embeddings", "mini", {"input": ["x"]}))["vectors"]
    finally:
        await e.close()


async def test_cancelled_request_does_not_leave_the_service_down(tmp_path, monkeypatch):
    e = restore_env(tmp_path, monkeypatch, "after_clean_shutdown")
    try:
        e.record_aux("embeddings", "mini", 1, 1)
        await e.load_aux("embeddings", "mini")
        pending = asyncio.create_task(e.aux.infer("embeddings", "mini", {"input": ["wait"]}))
        while not e.aux.services["embeddings"].busy:
            await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert not loaded(e, "embeddings")  # the worker's request stream is no longer trusted
        assert e.aux.memory.reserved_bytes == 0
        await e.aux.reconcile_once()
        assert loaded(e, "embeddings")  # recovered automatically
    finally:
        await e.close()


# ---- restore settings changed at runtime ----------------------------------------------------


async def test_restore_settings_can_be_changed_over_the_api_and_apply_live(env):
    async with env.client() as c:
        view = (await c.get("/api/services/settings", headers=ADMIN)).json()
        assert view["restore"] == "never" and view["overridden"] is False
        assert env.aux._task is None
        assert (await c.get("/api/services/settings", headers=INFER)).status_code == 403
        assert (
            await c.post("/api/services/settings", json={"restore": "on_demand"}, headers=INFER)
        ).status_code == 403
        r = await c.post(
            "/api/services/settings",
            json={"restore": "after_clean_shutdown", "restore_holdoff_seconds": 1800},
            headers=ADMIN,
        )
        assert r.status_code == 200
        assert (
            r.json()["restore"] == "after_clean_shutdown"
            and r.json()["restore_holdoff_seconds"] == 1800
        )
        assert r.json()["overridden"] is True and r.json()["configured_restore"] == "never"
        assert env.aux._task is not None  # the loop started without a restart
        assert env.aux.holdoff == 1800
        services = (await c.get("/api/services", headers=ADMIN)).json()
        assert services["embeddings"]["restore"]["policy"] == "after_clean_shutdown"
        assert services["embeddings"]["restore"]["holdoff_seconds"] == 1800
        r = await c.post("/api/services/settings", json={"restore": "never"}, headers=ADMIN)
        assert r.json()["restore"] == "never" and env.aux._task is None  # and stops again


async def test_restore_settings_reject_bad_input_and_change_nothing(env):
    async with env.client() as c:
        for body in (
            {},
            {"restore": "always"},
            {"restore_holdoff_seconds": -1},
            {"restore_holdoff_seconds": 8 * 24 * 3600},
            {"restore_holdoff_seconds": 1.5},
            {"restore_holdoff_seconds": "60"},
            {"unknown": 1},
            {"restore": "on_demand", "restore_holdoff_seconds": -5},
        ):
            r = await c.post("/api/services/settings", json=body, headers=ADMIN)
            assert r.status_code in (400, 422), (body, r.status_code)
        assert env.aux.policy == "never" and env.aux.holdoff == 3600 and env.aux._override == {}


async def test_saved_restore_settings_override_the_config_after_a_restart(tmp_path, monkeypatch):
    first = Env(tmp_path, monkeypatch)  # config says "never"
    await first.aux.set_settings("on_demand", 120)
    config = first.config
    await first.close()
    second = Env(tmp_path, monkeypatch, config=config)
    try:
        assert second.aux.policy == "on_demand" and second.aux.holdoff == 120
        assert second.aux.settings_view()["configured_restore"] == "never"
    finally:
        await second.close()
    # A damaged value in the saved state is ignored, not trusted. (Closing rewrites the
    # state, so the damage is injected afterwards.)
    state = json.loads((tmp_path / "auxiliary-state.json").read_text())
    state["settings"] = {"restore": "bogus", "restore_holdoff_seconds": -4}
    (tmp_path / "auxiliary-state.json").write_text(json.dumps(state))
    third = Env(tmp_path, monkeypatch, config=config)
    try:
        assert third.aux.policy == "never" and third.aux.holdoff == 3600
    finally:
        await third.close()


# ---- background loop and application lifespan ---------------------------------------------


async def test_reconcile_loop_restores_without_manual_calls(tmp_path, monkeypatch):
    e = restore_env(tmp_path, monkeypatch, "after_clean_shutdown")
    try:
        e.record_aux("embeddings", "mini", 1, 1)
        service = e.aux.services["embeddings"]
        service.enabled, service.desired_model = True, "mini"
        e.aux.start()
        assert e.aux._task is not None
        assert await wait_for(lambda: loaded(e, "embeddings"), 10)
    finally:
        await e.close()
    assert e.aux._task is None  # close() stops the loop


async def test_policy_never_does_not_start_the_loop(env):
    env.aux.start()
    assert env.aux._task is None


async def test_lifespan_starts_the_loop_and_marks_a_clean_shutdown(tmp_path, monkeypatch):
    e = restore_env(tmp_path, monkeypatch, "after_clean_shutdown")
    async with e.app.router.lifespan_context(e.app):
        assert e.aux._task is not None
        state = json.loads((tmp_path / "auxiliary-state.json").read_text())
        assert state["clean_shutdown"] is False  # a crash from here on is detectable
    assert e.aux._task is None
    assert json.loads((tmp_path / "auxiliary-state.json").read_text())["clean_shutdown"] is True


# ---- measurement of the workers -----------------------------------------------------------


async def test_worker_footprint_is_recorded_at_load_and_peak_after_requests(env):
    spec = env.aux.get("embeddings").models["mini"]
    fingerprint = aux_fingerprint("embeddings", "mini", spec)
    assert env.store.lookup("embeddings", "mini", fingerprint) is None
    await env.load_aux("embeddings", "mini")
    record = env.store.lookup("embeddings", "mini", fingerprint)
    assert record.source == "measured" and record.steady_bytes > 0 and record.requests_observed == 0
    await env.aux.infer("embeddings", "mini", {"input": ["a"]})
    record = env.store.lookup("embeddings", "mini", fingerprint)
    assert record.requests_observed >= 1 and record.peak_bytes >= record.steady_bytes
