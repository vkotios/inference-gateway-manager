"""Selection-scoped system prompts preserve client instructions and lifecycle state."""

import asyncio

import httpx
import pytest
from fastapi import HTTPException
from test_settings import settings_config

from gateway.app import create_app
from gateway.settings import apply_system_prompt


@pytest.mark.parametrize(
    ("path", "body", "expected"),
    [
        (
            "/v1/chat/completions",
            {"messages": [{"role": "user", "content": "hello"}]},
            {
                "messages": [
                    {"role": "system", "content": "Operator instruction"},
                    {"role": "user", "content": "hello"},
                ]
            },
        ),
        (
            "/v1/chat/completions",
            {
                "messages": [
                    {"role": "system", "content": "Client instruction"},
                    {"role": "user", "content": "hello"},
                ]
            },
            {
                "messages": [
                    {"role": "system", "content": "Operator instruction\n\nClient instruction"},
                    {"role": "user", "content": "hello"},
                ]
            },
        ),
        (
            "/v1/responses",
            {"input": "hello", "instructions": "Client instruction"},
            {"input": "hello", "instructions": "Operator instruction\n\nClient instruction"},
        ),
        (
            "/v1/messages",
            {"messages": [], "system": [{"type": "text", "text": "Client instruction"}]},
            {
                "messages": [],
                "system": [
                    {"type": "text", "text": "Operator instruction"},
                    {"type": "text", "text": "Client instruction"},
                ],
            },
        ),
    ],
)
def test_instructions_preserved(path, body, expected):
    assert apply_system_prompt(body, path, "Operator instruction") == expected


def test_empty_setting_is_transparent_and_unsupported_endpoint_is_explicit():
    body = {"prompt": "hello"}
    assert apply_system_prompt(body, "/v1/completions", "") == body
    with pytest.raises(HTTPException) as error:
        apply_system_prompt(body, "/v1/completions", "instruction")
    assert error.value.status_code == 422


@pytest.mark.asyncio
async def test_selection_prompt_proxy_clear_and_switch_without_implicit_reload(tmp_path):
    config = settings_config(tmp_path)
    app = create_app(config, "a" * 32, "b" * 32, tmp_path)
    manager = app.state.manager
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            admin = {"Authorization": "Bearer " + "a" * 32}
            inference = {"Authorization": "Bearer " + "b" * 32}
            first = await client.post(
                "/api/model",
                headers=admin,
                json={"engine": "one", "model": "fixture", "system_prompt": "Operator instruction"},
            )
            assert first.status_code == 200
            assert first.json()["active_settings"]["system_prompt"] == "Operator instruction"
            process, selection = manager.process, manager.selection_id
            await manager.http.aclose()

            class Body(httpx.AsyncByteStream):
                def __init__(self, data):
                    self.data = data

                async def __aiter__(self):
                    yield self.data

            async def fake_backend(request):
                if request.url.path == "/slots":
                    return httpx.Response(200, json=[{"is_processing": False, "n_ctx": 4096}])
                return httpx.Response(
                    200, headers={"Content-Type": "application/json"}, stream=Body(request.content)
                )

            manager.http = httpx.AsyncClient(transport=httpx.MockTransport(fake_backend))
            result = await client.post(
                "/v1/chat/completions",
                headers=inference,
                json={
                    "model": "fixture",
                    "messages": [
                        {"role": "system", "content": "Client instruction"},
                        {"role": "user", "content": "hello"},
                    ],
                },
            )
            assert result.status_code == 200
            assert (
                result.json()["messages"][0]["content"]
                == "Operator instruction\n\nClient instruction"
            )
            updated = await client.post(
                "/api/model", headers=admin, json={"model": "fixture", "system_prompt": ""}
            )
            assert updated.status_code == 200 and manager.process is process
            assert manager.selection_id != selection
            assert updated.json()["active_settings"]["system_prompt"] == ""
            stale = await client.post(
                "/v1/chat/completions",
                headers={**inference, "X-IGM-Selection-ID": selection},
                json={"model": "fixture", "messages": [{"role": "user", "content": "hello"}]},
            )
            assert stale.status_code == 409
            result = await client.post(
                "/v1/chat/completions",
                headers=inference,
                json={"model": "fixture", "messages": [{"role": "user", "content": "hello"}]},
            )
            assert result.json()["messages"] == [{"role": "user", "content": "hello"}]
            repeated = await client.post("/api/model", headers=admin, json={"model": "fixture"})
            assert repeated.json()["active_settings"]["system_prompt"] == ""
            unsupported = await client.post(
                "/api/model", headers=admin, json={"model": "fixture", "system_prompt": "x" * 16385}
            )
            assert unsupported.status_code == 422 and manager.process is process
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_disabled_capability_refuses_prompt_before_stopping_current_model(tmp_path):
    cfg = settings_config(tmp_path)
    cfg["engines"]["two"]["models"]["other"]["system_prompt_supported"] = False
    app = create_app(cfg, "a" * 32, "b" * 32, tmp_path)
    m = app.state.manager
    try:
        await m.load_model("fixture", engine="one", system_prompt="Keep")
        process = m.process
        with pytest.raises(HTTPException) as error:
            await m.load_model("other", engine="two", system_prompt="Unsupported")
        assert error.value.status_code == 422 and m.process is process
    finally:
        await m.close()


@pytest.mark.asyncio
async def test_prompt_can_change_without_reloading_a_fixed_reasoning_model(tmp_path):
    cfg = settings_config(tmp_path)
    spec = cfg["engines"]["one"]["models"]["fixture"]
    spec["reasoning_modes"] = ["auto"]
    spec["reasoning_change"] = "fixed"
    spec.pop("reasoning_defaults")
    app = create_app(cfg, "a" * 32, "b" * 32, tmp_path)
    manager = app.state.manager
    try:
        await manager.load_model("fixture", engine="one")
        process = manager.process
        await manager.load_model("fixture", system_prompt="Be concise.")
        assert manager.process is process
        assert manager.active_settings["system_prompt"] == "Be concise."
        await manager.load_model("other", engine="two")
        assert manager.active_settings["system_prompt"] == ""
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_prompt_update_keeps_admitted_request_and_invalidates_future_stale_requests(tmp_path):
    app = create_app(settings_config(tmp_path), "a" * 32, "b" * 32, tmp_path)
    manager = app.state.manager
    admitted, release = asyncio.Event(), asyncio.Event()
    task = None
    try:
        await manager.load_model("fixture", engine="one", system_prompt="Original instruction")
        process, selection = manager.process, manager.selection_id
        await manager.http.aclose()

        class EchoBody(httpx.AsyncByteStream):
            def __init__(self, content):
                self.content = content

            async def __aiter__(self):
                yield self.content

        async def backend(request):
            if request.url.path == "/slots":
                return httpx.Response(200, json=[{"is_processing": False, "n_ctx": 4096}])
            admitted.set()
            await release.wait()
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                stream=EchoBody(request.content),
            )

        manager.http = httpx.AsyncClient(transport=httpx.MockTransport(backend))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            task = asyncio.create_task(
                client.post(
                    "/v1/chat/completions",
                    headers={
                        "Authorization": "Bearer " + "b" * 32,
                        "X-IGM-Selection-ID": selection,
                    },
                    json={"model": "fixture", "messages": [{"role": "user", "content": "hello"}]},
                )
            )
            await asyncio.wait_for(admitted.wait(), 5)
            await manager.load_model("fixture", system_prompt="New instruction")
            assert manager.process is process and manager.active
            assert manager.selection_id != selection
            await manager.load_model("fixture")
            assert manager.active_settings["system_prompt"] == "New instruction"
            release.set()
            response = await asyncio.wait_for(task, 5)
            assert response.json()["messages"][0]["content"] == "Original instruction"
            response = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer " + "b" * 32},
                json={"model": "fixture", "messages": [{"role": "user", "content": "next"}]},
            )
            assert response.json()["messages"][0]["content"] == "New instruction"
    finally:
        release.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await manager.close()
