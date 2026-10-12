import asyncio

import httpx
import pytest

from gateway.app import create_app


@pytest.mark.asyncio
async def test_inference_does_not_queue_behind_lifecycle(tmp_path):
    app = create_app(
        {"allowed_hosts": ["testserver"], "engines": {"one": {"models": {}}}},
        "a" * 32,
        "b" * 32,
        tmp_path,
    )
    m = app.state.manager
    await m.lock.acquire()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": "Bearer " + "b" * 32},
        ) as c:
            r = await asyncio.wait_for(
                c.post("/v1/chat/completions", json={"model": "fixture", "messages": []}),
                timeout=0.25,
            )
            assert r.status_code == 409
    finally:
        m.lock.release()
        await m.close()
