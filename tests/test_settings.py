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
            "slots": 1,
            "context_source": "native_slots",
            "reasoning": "off",
            "system_prompt": "",
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


def slots_spec(tmp_path, **extra):
    spec = settings_config(tmp_path)["engines"]["one"]["models"]["fixture"]
    spec["context"] = 131072
    spec["context_limits"] = {
        "min": 1024,
        "max": 262144,
        "presets": [65536, 131072, 262144],
        "adjustable": True,
    }
    spec["argv"] += ["-e", "POOL={slots_x_context}", "-e", "SLOTS={slots}"]
    spec.update(extra)
    return spec


def test_select_renders_slots_and_slots_x_context_in_argv_and_config_template(tmp_path):
    from gateway.settings import select

    spec = slots_spec(
        tmp_path,
        chat_slots=3,
        config_template={
            "pool": "X={slots_x_context}",
            "nested": [{"n": "N={slots}", "c": "C={context}"}, "keep", 7],
        },
    )
    chosen, settings = select(spec, 131072)
    assert settings["context"] == 131072
    assert chosen["argv"][-4:] == ["-e", "POOL=393216", "-e", "SLOTS=3"]
    assert "131072" in chosen["argv"]  # the {context} binding is unchanged
    assert chosen["config_template"] == {
        "pool": "X=393216",
        "nested": [{"n": "N=3", "c": "C=131072"}, "keep", 7],
    }
    assert chosen["_config_template"]["pool"] == "X={slots_x_context}"  # the template is kept
    smaller, _ = select(spec, 65536)
    assert smaller["argv"][-4:] == ["-e", "POOL=196608", "-e", "SLOTS=3"]
    assert smaller["config_template"]["pool"] == "X=196608"
    # without chat_slots a model has one slot
    single, _ = select(slots_spec(tmp_path), 131072)
    assert single["argv"][-4:] == ["-e", "POOL=131072", "-e", "SLOTS=1"]


def test_slots_x_context_needs_a_declared_context():
    from gateway.settings import select

    spec = {"argv": ["engine", "--pool", "{slots_x_context}"], "base_url": "http://127.0.0.1:1"}
    with pytest.raises(HTTPException) as refused:
        select(spec)
    assert refused.value.status_code == 422 and "declared context" in refused.value.detail
    # {slots} alone needs no context
    chosen, _ = select({**spec, "argv": ["engine", "--slots", "{slots}"], "chat_slots": 2})
    assert chosen["argv"] == ["engine", "--slots", "2"]


@pytest.mark.asyncio
async def test_context_change_rerenders_slots_x_context_from_the_template(tmp_path):
    cfg = settings_config(tmp_path)
    cfg["engines"]["one"]["models"]["fixture"] = slots_spec(tmp_path, chat_slots=3)
    m = Manager(cfg, tmp_path)
    try:
        engine, first, settings = m.prepare_model("fixture", engine="one", context=131072)
        assert "POOL=393216" in first["argv"]
        # the model is now "loaded": its argv is frozen (rendered) and only the template can be re-rendered
        m.engine, m.model, m.active_spec, m.selected_settings = engine, "fixture", first, settings
        _, second, again = m.prepare_model("fixture", context=65536)
        assert again["context"] == 65536
        assert "POOL=196608" in second["argv"] and "POOL=393216" not in second["argv"]
        assert "POOL={slots_x_context}" in second["_argv_template"]
        # omitted settings keep the selected context, and the slot product follows it
        _, third, kept = m.prepare_model("fixture")
        assert kept["context"] == 131072 and "POOL=393216" in third["argv"]
        assert "POOL={slots_x_context}" in cfg["engines"]["one"]["models"]["fixture"]["argv"]
    finally:
        await m.close()


# ---- slot selection ---------------------------------------------------------------------------


def adjustable_slots_spec(tmp_path, **extra):
    spec = settings_config(tmp_path)["engines"]["one"]["models"]["fixture"]
    spec["context"] = 262144
    spec["context_limits"] = {
        "min": 1024,
        "max": 262144,
        "presets": [65536, 262144],
        "adjustable": True,
    }
    spec["argv"] += ["--parallel", "{slots}", "--ctx-size", "{slots_x_context}"]
    spec["chat_slots"] = 1
    spec["slot_limits"] = {"min": 1, "max": 4, "presets": [1, 2, 3, 4], "adjustable": True}
    spec.update(extra)
    return spec


def test_slots_x_context_is_a_context_binding(tmp_path):
    from gateway.settings import validate_capabilities

    spec = adjustable_slots_spec(tmp_path)
    spec["argv"] = [arg for arg in spec["argv"] if "{context}" not in arg]
    validate_capabilities(spec)


def test_slot_limits_default_to_fixed_at_chat_slots_and_appear_in_the_summary(tmp_path):
    from gateway.settings import capabilities, summary

    spec = settings_config(tmp_path)["engines"]["one"]["models"]["fixture"]
    assert capabilities(spec)["slot_limits"] == {
        "min": 1,
        "max": 1,
        "presets": [1],
        "adjustable": False,
    }
    three = {**spec, "chat_slots": 3}
    assert capabilities(three)["slot_limits"] == {
        "min": 3,
        "max": 3,
        "presets": [3],
        "adjustable": False,
    }
    shown = summary("fixture", adjustable_slots_spec(tmp_path))
    assert shown["chat_slots"] == 1
    assert shown["slot_limits"] == {"min": 1, "max": 4, "presets": [1, 2, 3, 4], "adjustable": True}


@pytest.mark.parametrize(
    "change,message",
    [
        (
            {
                "slot_limits": {"min": 1, "max": 4, "presets": [1, 2], "adjustable": True},
                "argv": ["x", "{context}"],
            },
            "{slots}",
        ),
        ({"chat_slots": 5}, "slot range"),
        ({"chat_slots": 0}, "chat_slots"),
        ({"slot_limits": {"min": 2, "max": 4, "presets": [2], "adjustable": True}}, "slot range"),
        ({"slot_limits": {"min": 1, "max": 4, "presets": [1, 9], "adjustable": True}}, "presets"),
        ({"slot_limits": {"min": 1, "max": 4, "presets": [1], "adjustable": "yes"}}, "slot limits"),
        ({"slot_limits": {"min": 1, "max": 4, "presets": [1]}}, "requires min"),
        ({"slot_limits": {"min": 1, "max": 2, "presets": [1], "adjustable": False}}, "Fixed"),
        (
            {"slot_limits": {"min": True, "max": 4, "presets": [1], "adjustable": True}},
            "slot range",
        ),
    ],
)
def test_invalid_slot_limits_are_refused(tmp_path, change, message):
    from gateway.settings import select, validate_capabilities

    spec = adjustable_slots_spec(tmp_path, **change)
    with pytest.raises(ValueError, match=message.replace("{", r"\{").replace("}", r"\}")):
        validate_capabilities(spec)
    with pytest.raises(HTTPException) as refused:
        select(spec)
    assert refused.value.status_code == 422 and "Invalid model capabilities" in refused.value.detail


def test_adjustable_slots_may_bind_slots_in_a_config_template(tmp_path):
    from gateway.settings import select, validate_capabilities

    spec = adjustable_slots_spec(tmp_path, config_template={"slots": "{slots}"})
    spec["argv"] = [a for a in spec["argv"] if "{slots}" not in a]
    spec["argv"] = [a for a in spec["argv"] if a != "--parallel"]
    validate_capabilities(spec)
    chosen, settings = select(spec, 65536, slots=3)
    assert chosen["config_template"] == {"slots": "3"} and settings["slots"] == 3


def test_select_with_slots_renders_parallel_and_the_pool_and_freezes_chat_slots(tmp_path):
    from gateway.settings import select

    spec = adjustable_slots_spec(tmp_path)
    chosen, settings = select(spec, 262144, slots=2)
    argv = chosen["argv"]
    assert argv[argv.index("--parallel") + 1] == "2"
    assert argv[argv.index("--ctx-size") + 1] == "524288"  # 2 x 262,144
    assert chosen["chat_slots"] == 2 and settings["slots"] == 2 and settings["context"] == 262144
    assert spec["chat_slots"] == 1  # the administrator's entry is not changed
    # omitted: the model's own count, so a reload at one slot is the command line it has always had
    default, default_settings = select(spec, 262144)
    assert default_settings["slots"] == 1
    assert default["argv"][default["argv"].index("--ctx-size") + 1] == "262144"
    assert select(spec, 65536, slots=4)[0]["argv"][-1] == "262144"  # 4 x 65,536


@pytest.mark.parametrize("slots", [0, 5, -1, True, "2", 2.0])
def test_slots_outside_the_limits_or_of_the_wrong_type_are_422(tmp_path, slots):
    from gateway.settings import select

    with pytest.raises(HTTPException) as refused:
        select(adjustable_slots_spec(tmp_path), slots=slots)
    assert refused.value.status_code == 422
    assert refused.value.detail == "Slot count is not supported by this model"


def test_a_model_without_adjustable_slots_accepts_only_its_own_count(tmp_path):
    from gateway.settings import select

    spec = {**settings_config(tmp_path)["engines"]["one"]["models"]["fixture"], "chat_slots": 3}
    assert select(spec, slots=3)[1]["slots"] == 3
    with pytest.raises(HTTPException) as refused:
        select(spec, slots=2)
    assert refused.value.status_code == 422


def record_slots(manager, slots, context, need):
    from gateway.footprints import CHAT, Footprint, chat_fingerprint

    _, spec, _ = manager.prepare_model("fixture", "one", context, slots=slots)
    return manager.tracker.store.record(
        Footprint(
            CHAT,
            "one/fixture",
            need // 2,
            need,
            context=context,
            fingerprint=chat_fingerprint("one", "fixture", spec),
            note=f"slots={slots}",
        )
    )


def slots_app(tmp_path, native_slots=4, *, measured=True, **extra):
    import json

    cfg = settings_config(tmp_path)
    cfg["engines"]["one"]["models"]["fixture"] = adjustable_slots_spec(
        tmp_path, admission={"reservedInteractive": 1}, **extra
    )
    # what the fixture backend reports on /slots: one entry per native slot
    (tmp_path / "slots.json").write_text(
        json.dumps(
            [{"id": i, "is_processing": False, "n_ctx": 262144} for i in range(native_slots)]
        )
    )
    app = create_app(cfg, "a" * 32, "b" * 32, tmp_path)
    manager = app.state.manager
    manager.auxiliary.sample_total = lambda: 256 * 1024**3
    manager.sample_available = lambda: 128 * 1024**3
    if measured:
        for count in range(2, 5):
            record_slots(manager, count, 262144, 64 * 1024**2)
    return app


def admin(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Authorization": "Bearer " + "a" * 32},
    )


@pytest.mark.asyncio
async def test_a_slot_change_restarts_the_engine_and_other_changes_do_not(tmp_path):
    app = slots_app(tmp_path)
    manager = app.state.manager
    try:
        async with admin(app) as client:
            first = await client.post(
                "/api/model", json={"engine": "one", "model": "fixture", "slots": 2}
            )
            assert first.status_code == 200
            assert first.json()["active_settings"]["slots"] == 2
            process = manager.process
            # the same slots with a changed reasoning default: applied to the running engine
            changed = await client.post("/api/model", json={"model": "fixture", "reasoning": "on"})
            assert changed.status_code == 200 and manager.process is process
            assert changed.json()["active_settings"]["slots"] == 2
            # slots omitted: the selected count is kept, nothing restarts
            kept = await client.post("/api/model", json={"model": "fixture"})
            assert kept.status_code == 200 and manager.process is process
            assert kept.json()["active_settings"]["slots"] == 2
            # the same count stated again is not a change either
            same = await client.post("/api/model", json={"model": "fixture", "slots": 2})
            assert same.status_code == 200 and manager.process is process
            # a different count restarts the engine
            more = await client.post("/api/model", json={"model": "fixture", "slots": 3})
            assert more.status_code == 200 and manager.process is not process
            assert more.json()["active_settings"]["slots"] == 3
            assert (
                more.json()["active_settings"]["reasoning"] == "on"
            )  # the other settings carry over
            # a context change keeps the slot count, and the count in the launch follows it
            resized = await client.post("/api/model", json={"model": "fixture", "context": 65536})
            assert resized.json()["active_settings"]["slots"] == 3
            assert manager.active_spec["chat_slots"] == 3
            assert manager.active_spec["argv"][-1] == str(3 * 65536)
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_slots_outside_the_limits_are_refused_without_touching_the_loaded_model(tmp_path):
    app = slots_app(tmp_path)
    manager = app.state.manager
    try:
        async with admin(app) as client:
            assert (
                await client.post(
                    "/api/model", json={"engine": "one", "model": "fixture", "slots": 2}
                )
            ).status_code == 200
            process = manager.process
            for bad in (0, 5, "2", True, 2.5):
                refused = await client.post("/api/model", json={"model": "fixture", "slots": bad})
                assert refused.status_code == 422, bad
            assert manager.process is process and manager.active_settings["slots"] == 2
            assert (await client.post("/api/model", json={"model": "fixture"})).status_code == 200
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_chat_capacity_and_the_broker_follow_the_selected_slots(tmp_path):
    app = slots_app(tmp_path, native_slots=4)
    manager = app.state.manager
    try:
        async with admin(app) as client:
            three = await client.post(
                "/api/model", json={"engine": "one", "model": "fixture", "slots": 3}
            )
            assert three.json()["chat_slots"]["total"] == 3
            admission = (await client.get("/api/admission")).json()
            assert admission["mode"] == "queued" and admission["slots_total"] == 3
            assert admission["reserved_now"] == 1  # the configured reservation, within 3 slots
            one = await client.post("/api/model", json={"model": "fixture", "slots": 1})
            assert one.json()["chat_slots"]["total"] == 1
            admission = (await client.get("/api/admission")).json()
            assert admission["mode"] == "single" and admission["slots_total"] == 1
            assert admission["reserved_now"] == 0  # a single slot is never reserved away
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_chat_capacity_is_capped_by_the_slots_the_engine_reports(tmp_path):
    app = slots_app(tmp_path, native_slots=2)
    manager = app.state.manager
    try:
        async with admin(app) as client:
            loaded = await client.post(
                "/api/model", json={"engine": "one", "model": "fixture", "slots": 4}
            )
            assert loaded.json()["active_settings"]["slots"] == 4
            assert loaded.json()["chat_slots"]["total"] == 2  # the engine only has two
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_without_slots_a_load_behaves_as_before(tmp_path):
    cfg = settings_config(tmp_path)
    app = create_app(cfg, "a" * 32, "b" * 32, tmp_path)
    manager = app.state.manager
    try:
        async with admin(app) as client:
            loaded = await client.post("/api/model", json={"engine": "one", "model": "fixture"})
            assert loaded.status_code == 200
            assert loaded.json()["active_settings"]["slots"] == 1
            process = manager.process
            assert (await client.post("/api/model", json={"model": "fixture"})).status_code == 200
            assert manager.process is process
            refused = await client.post("/api/model", json={"model": "fixture", "slots": 2})
            assert refused.status_code == 422  # not adjustable: only its own count
            catalog = (await client.get("/api/engines")).json()["engines"][0]["models"][0]
            assert catalog["slot_limits"]["adjustable"] is False and catalog["chat_slots"] == 1
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_prepare_model_inherits_the_selected_slots_on_a_context_change(tmp_path):
    cfg = settings_config(tmp_path)
    cfg["engines"]["one"]["models"]["fixture"] = adjustable_slots_spec(tmp_path)
    m = Manager(cfg, tmp_path)
    try:
        engine, first, settings = m.prepare_model("fixture", engine="one", context=262144, slots=2)
        assert settings["slots"] == 2
        m.engine, m.model, m.active_spec, m.selected_settings = engine, "fixture", first, settings
        _, second, again = m.prepare_model("fixture", context=65536)
        assert again["slots"] == 2 and second["argv"][-1] == str(2 * 65536)
        # an explicit count wins, and the frozen spec's own count never leaks into a fresh load
        _, third, explicit = m.prepare_model("fixture", slots=4)
        assert explicit["slots"] == 4 and third["chat_slots"] == 4
        m.engine = m.model = m.active_spec = m.selected_settings = None
        _, fresh, default = m.prepare_model("fixture", engine="one")
        assert default["slots"] == 1 and fresh["chat_slots"] == 1
    finally:
        await m.close()


@pytest.mark.asyncio
async def test_footprint_view_stays_at_the_default_slots_and_shows_a_record_per_slot_count(
    tmp_path,
):
    app = slots_app(tmp_path, measured=False)
    manager = app.state.manager
    try:
        async with admin(app) as client:
            view = (await client.get("/api/footprints")).json()
            entry = next(c for c in view["compatibility"] if c["model"] == "fixture")
            # the list is always at the model's own count, whatever is loaded now
            assert entry["slots"] == 1 and entry["calibrated"] is False
            assert sorted(entry["by_slots"]) == ["1", "2", "3", "4"]
            assert not any(entry["by_slots"].values())
            # a measurement at 2 slots covers 2 slots and nothing else
            record_slots(manager, 2, 262144, 41 * 1024**3)
            loaded = await client.post(
                "/api/model", json={"engine": "one", "model": "fixture", "slots": 2}
            )
            assert loaded.status_code == 200, loaded.text
            view = (await client.get("/api/footprints")).json()
            entry = next(c for c in view["compatibility"] if c["model"] == "fixture")
            assert entry["calibrated"] is False
            assert [k for k, records in entry["by_slots"].items() if records] == ["2"]
            assert entry["by_slots"]["2"] == [{"context": 262144, "needs_bytes": 41 * 1024**3}]
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_calibrate_reloads_at_the_default_slots(tmp_path):
    app = slots_app(tmp_path)
    manager = app.state.manager
    app.state.manager.tracker.settle_seconds = 0.0
    app.state.manager.tracker.interval = 0.0
    try:
        async with admin(app) as client:
            await client.post("/api/model", json={"engine": "one", "model": "fixture", "slots": 3})
            assert manager.active_settings["slots"] == 3
            calibrated = await client.post(
                "/api/footprints/calibrate", json={"engine": "one", "model": "fixture"}
            )
            assert manager.active_settings["slots"] == 1, calibrated.text
    finally:
        await manager.close()
