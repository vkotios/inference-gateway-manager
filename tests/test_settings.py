import asyncio
import copy
import socket
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

from gateway.app import create_app
from gateway.core import Manager


def test_unmapped_reasoning_default_is_refused_instead_of_silently_ignored():
    from gateway.settings import apply_reasoning

    spec = {
        "reasoning_change": "request",
        "reasoning_defaults": {"off": {"/v1/chat/completions": {"reasoning_effort": "none"}}},
    }
    with pytest.raises(HTTPException) as error:
        apply_reasoning({"input": "Hello"}, "/v1/responses", spec, "off")
    assert error.value.status_code == 422
    explicit = {"reasoning": {"effort": "high"}}
    assert apply_reasoning(explicit, "/v1/responses", spec, "off") == explicit


def test_explicit_template_reasoning_effort_never_receives_conflicting_default():
    from gateway.settings import apply_reasoning

    spec = {
        "reasoning_change": "request",
        "reasoning_defaults": {
            "on": {"/v1/chat/completions": {"chat_template_kwargs": {"enable_thinking": True}}}
        },
    }
    body = {"chat_template_kwargs": {"reasoning_effort": "none"}}
    assert apply_reasoning(body, "/v1/chat/completions", spec, "on") == {
        "chat_template_kwargs": {"reasoning_effort": "none"}
    }


def test_non_control_reasoning_options_keep_default_effort():
    from gateway.settings import apply_reasoning

    spec = {
        "reasoning_change": "request",
        "reasoning_defaults": {"off": {"/v1/responses": {"reasoning": {"effort": "none"}}}},
    }
    body = {"reasoning": {"summary": "auto"}}
    assert apply_reasoning(body, "/v1/responses", spec, "off") == {
        "reasoning": {"summary": "auto", "effort": "none"}
    }


@pytest.mark.asyncio
async def test_old_slot_probe_cannot_overwrite_new_model_context(tmp_path):
    m = Manager({"engines": {"one": {"models": {}}}}, tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    async def backend(request):
        entered.set()
        await release.wait()
        return httpx.Response(200, json=[{"is_processing": False, "n_ctx": 32768}])

    await m.http.aclose()
    m.http = httpx.AsyncClient(transport=httpx.MockTransport(backend))
    m.engine, m.model, m.phase = "one", "old", "ready"
    m.launch_id = "old-launch"
    m.active_spec = {"base_url": "http://127.0.0.1:8080", "probe": "slots"}
    m.active_settings = {"context": 8192, "context_source": "declared", "reasoning": "auto"}
    pending = asyncio.create_task(m.native_busy())
    try:
        await entered.wait()
        m.reset_loaded()
        m.model, m.phase, m.launch_id = "new", "ready", "new-launch"
        m.active_spec = {"base_url": "http://127.0.0.1:8081", "probe": "gateway_only"}
        m.active_settings = {"context": 4096, "context_source": "declared", "reasoning": "auto"}
        release.set()
        assert await pending is None
        assert m.active_settings["context"] == 4096
        assert m.active_settings["context_source"] == "declared"
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        m.model = None
        await m.close()


def settings_config(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    spec = {
        "argv": [
            sys.executable,
            str(Path(__file__).with_name("fixture_backend.py")),
            str(port),
            str(tmp_path),
            "{context}",
        ],
        "base_url": f"http://127.0.0.1:{port}",
        "context": 8192,
        "context_limits": {
            "min": 1024,
            "max": 16384,
            "presets": [4096, 8192, 16384],
            "adjustable": True,
        },
        "reasoning_modes": ["auto", "on", "off"],
        "reasoning_change": "request",
        "reasoning_defaults": {
            "on": {"/v1/chat/completions": {"chat_template_kwargs": {"enable_thinking": True}}},
            "off": {"/v1/chat/completions": {"chat_template_kwargs": {"enable_thinking": False}}},
        },
    }
    return {
        "engines": {
            "one": {"models": {"fixture": spec}},
            "two": {"models": {"other": {**copy.deepcopy(spec), "context": 8192}}},
        },
        "startup_timeout": 5,
        "allowed_hosts": ["testserver"],
    }


@pytest.mark.asyncio
async def test_repeated_load_inherits_omitted_settings_without_restarting(tmp_path):
    app = create_app(settings_config(tmp_path), "a" * 32, "b" * 32, tmp_path)
    manager = app.state.manager
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": "Bearer " + "a" * 32},
        ) as client:
            first = await client.post(
                "/api/model",
                json={
                    "engine": "one",
                    "model": "fixture",
                    "context": 6144,
                    "reasoning": "off",
                },
            )
            assert first.status_code == 200
            process, selection = manager.process, manager.selection_id
            repeated = await client.post("/api/model", json={"model": "fixture"})
            assert repeated.status_code == 200
            assert manager.process is process
            assert manager.selection_id == selection
            assert repeated.json()["active_settings"]["requested_context"] == 6144
            assert repeated.json()["active_settings"]["reasoning"] == "off"
            changed = await client.post("/api/model", json={"model": "fixture", "reasoning": "on"})
            assert changed.status_code == 200 and manager.process is process
            assert changed.json()["active_settings"]["requested_context"] == 6144
            resized = await client.post("/api/model", json={"model": "fixture", "context": 4096})
            assert resized.status_code == 200 and manager.process is not process
            assert resized.json()["active_settings"]["reasoning"] == "on"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_catalog_and_fixed_legacy_capabilities(tmp_path):
    cfg = settings_config(tmp_path)
    cfg["engines"]["one"]["models"]["fixture"]["reasoning"] = "off"
    cfg["engines"]["two"]["models"]["other"] = {
        "argv": ["/bin/false"],
        "base_url": "http://127.0.0.1:8089",
        "context": 2048,
    }
    app = create_app(cfg, "a" * 32, "b" * 32, tmp_path)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": "Bearer " + "a" * 32},
        ) as c:
            catalog = (await c.get("/api/engines")).json()["engines"]
            assert catalog[0]["models"][0]["reasoning"] == "off"
            legacy = catalog[1]["models"][0]
            assert legacy["reasoning"] == "auto"
            assert legacy["context_limits"] == {
                "min": 2048,
                "max": 2048,
                "presets": [2048],
                "adjustable": False,
            }
            assert legacy["reasoning_modes"] == ["auto"]
            assert legacy["reasoning_change"] == "fixed"
    finally:
        await app.state.manager.close()


@pytest.mark.asyncio
async def test_settings_restart_readback_frozen_spec_and_invalid_preflight(tmp_path):
    cfg = settings_config(tmp_path)
    m = Manager(cfg, tmp_path)
    try:
        await m.load_model("fixture", engine="one", context=8192, reasoning="off")
        process = m.process
        selection = m.selection_id
        assert m.active_settings == {
            "context": 4096,
            "requested_context": 8192,
            "context_source": "native_slots",
            "reasoning": "off",
        }
        await m.load_model("fixture", context=8192, reasoning="off")
        assert m.process is process and m.selection_id == selection
        with pytest.raises(HTTPException) as error:
            await m.load_model("other", engine="two", context=999999)
        assert error.value.status_code == 422
        assert m.process is process and m.engine == "one"
        await m.load_model("fixture", context=8192, reasoning="on")
        assert m.process is process and m.selection_id != selection
        cfg["engines"]["one"]["models"]["fixture"]["base_url"] = "http://127.0.0.1:1"
        assert m.spec()["base_url"] != "http://127.0.0.1:1"
        await m.load_model("fixture", context=4096, reasoning="on")
        assert process.poll() is not None and m.process is not process
    finally:
        await m.close()


@pytest.mark.asyncio
async def test_selection_guard_reasoning_precedence_and_raw_stream(tmp_path):
    app = create_app(settings_config(tmp_path), "a" * 32, "b" * 32, tmp_path)
    m = app.state.manager
    seen = []
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": "Bearer " + "a" * 32},
        ) as c:
            loaded = await c.post(
                "/api/model", json={"engine": "one", "model": "fixture", "reasoning": "off"}
            )
            assert loaded.status_code == 200
            selection = loaded.json()["selection_id"]
            discovered = (await c.get("/v1/models")).json()["data"]
            assert discovered[0]["context_length"] == 4096
            original = m.http.send

            async def capture(request, **kwargs):
                if request.method == "POST":
                    import json

                    seen.append(json.loads(request.content))
                return await original(request, **kwargs)

            m.http.send = capture
            stale = await c.post(
                "/v1/chat/completions",
                headers={"X-IGM-Selection-ID": "old"},
                json={"model": "fixture", "messages": []},
            )
            assert stale.status_code == 409
            for extra in ({}, {"chat_template_kwargs": {"enable_thinking": True}}):
                response = await c.post(
                    "/v1/chat/completions",
                    headers={"X-IGM-Selection-ID": selection},
                    json={"model": "fixture", "messages": [], **extra},
                )
                assert response.text == "data: fixture\n\ndata: [DONE]\n\n"
            assert seen[0]["chat_template_kwargs"]["enable_thinking"] is False
            assert seen[1]["chat_template_kwargs"]["enable_thinking"] is True
    finally:
        await m.close()


@pytest.mark.asyncio
async def test_invalid_force_settings_do_not_cancel_current_loading_task(tmp_path):
    m = Manager(settings_config(tmp_path), tmp_path)
    task = asyncio.create_task(asyncio.sleep(30))
    m.loading_task = task
    m.phase = "loading"
    m.engine = "one"
    try:
        with pytest.raises(HTTPException) as error:
            await m.load_model("fixture", force=True, context=999999)
        assert error.value.status_code == 422
        assert not task.cancelled() and not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        m.loading_task = None
        m.phase = "selected"
        await m.close()


@pytest.mark.parametrize(
    "path,defaults,explicit",
    [
        (
            "/v1/chat/completions",
            {"chat_template_kwargs": {"enable_thinking": False}},
            {"reasoning_effort": "high"},
        ),
        ("/v1/responses", {"reasoning": {"effort": "none"}}, {"reasoning": {"effort": "high"}}),
        (
            "/v1/messages",
            {"thinking": {"type": "disabled"}},
            {"thinking": {"type": "enabled", "budget_tokens": 2048}},
        ),
    ],
)
def test_each_protocol_respects_explicit_client_reasoning(path, defaults, explicit):
    from gateway.settings import apply_reasoning

    spec = {"reasoning_defaults": {"off": {path: defaults}}}
    assert apply_reasoning({}, path, spec, "off") == defaults
    assert apply_reasoning(dict(explicit), path, spec, "off") == explicit


@pytest.mark.asyncio
async def test_legacy_unsupported_controls_preserve_process(tmp_path):
    cfg = settings_config(tmp_path)
    spec = cfg["engines"]["one"]["models"]["fixture"]
    spec.pop("context_limits")
    spec["argv"] = [arg.replace("{context}", "8192") for arg in spec["argv"]]
    for key in ("reasoning_modes", "reasoning_change", "reasoning_defaults"):
        spec.pop(key)
    m = Manager(cfg, tmp_path)
    try:
        await m.load_model("fixture", engine="one")
        process = m.process
        for options in ({"context": 4096}, {"reasoning": "off"}):
            with pytest.raises(HTTPException) as error:
                await m.load_model("fixture", **options)
            assert error.value.status_code == 422
            assert process.poll() is None and m.process is process
    finally:
        await m.close()


@pytest.mark.asyncio
async def test_restart_reasoning_and_atomic_cross_engine_transition(tmp_path):
    cfg = settings_config(tmp_path)
    spec = cfg["engines"]["two"]["models"]["other"]
    spec.update(
        reasoning_change="restart",
        reasoning_argv={"on": ["--thinking-on"], "off": ["--thinking-off"]},
    )
    m = Manager(cfg, tmp_path)
    try:
        await m.load_model("fixture", engine="one")
        old = m.process
        await m.load_model("other", engine="two", reasoning="on")
        assert old.poll() is not None
        assert m.engine == "two" and m.model == "other"
        assert "--thinking-on" in m.spec()["argv"]
        current = m.process
        await m.load_model("other", reasoning="off")
        assert current.poll() is not None and m.process is not current
        assert "--thinking-off" in m.spec()["argv"] and "--thinking-on" not in m.spec()["argv"]
    finally:
        await m.close()


@pytest.mark.asyncio
async def test_strata_private_generated_config_keeps_trusted_args_and_context(tmp_path):
    import json

    cfg = settings_config(tmp_path)
    spec = cfg["engines"]["one"]["models"]["fixture"]
    spec["argv"][-1] = "{config_path}"
    spec["config_template"] = {
        "exe": "/fixed/native-engine",
        "args": ["--pack", "/fixed/pack", "--max-context", "{context}"],
        "tokenizer": "/fixed/tokenizer",
    }
    original = copy.deepcopy(spec["config_template"])
    m = Manager(cfg, tmp_path)
    try:
        await m.load_model("fixture", engine="one", context=4096)
        generated = m.launch_config_path
        assert generated.parent == tmp_path and generated.stat().st_mode & 0o777 == 0o600
        assert json.loads(generated.read_text())["args"] == [
            "--pack",
            "/fixed/pack",
            "--max-context",
            "4096",
        ]
        assert spec["config_template"] == original
        await m.load_model("fixture", context=8192)
        assert not generated.exists()
        assert json.loads(m.launch_config_path.read_text())["args"][-1] == "8192"
    finally:
        await m.close()
    assert not list(tmp_path.glob("launch-*.json"))
