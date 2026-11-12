import asyncio
import base64
import io
import shutil
import struct
import sys
import wave
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

from gateway.app import create_app


@pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("prlimit")),
    reason="decoding the uploaded audio needs ffmpeg and prlimit",
)
@pytest.mark.asyncio
async def test_services_control_auth_inference_and_memory_gate(tmp_path, monkeypatch):
    path = str(tmp_path)
    spec = {"path": path, "load_bytes": 100, "request_bytes": 10}
    config = {
        "allowed_hosts": ["testserver"],
        "engines": {"one": {"models": {}}},
        "auxiliary": {
            "minimum_free_bytes": 10,
            "embeddings": {"models": {"mini": spec}},
            "transcription": {"models": {"whisper": spec}},
        },
    }
    app = create_app(config, "a" * 32, "b" * 32, tmp_path)
    aux = app.state.auxiliary
    aux.memory._sample = lambda: 128 * 1024**3
    aux.sample_total = lambda: 128 * 1024**3
    native = asyncio.create_subprocess_exec
    fixture = str(Path(__file__).with_name("fixture_aux_worker.py"))

    async def fixture_process(*args, **kwargs):
        if len(args) > 2 and args[1].endswith("/gateway/aux_worker.py"):
            args = (sys.executable, fixture, args[2])
        return await native(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fixture_process)
    admin = {"Authorization": "Bearer " + "a" * 32}
    infer = {"Authorization": "Bearer " + "b" * 32}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        assert (
            await c.post("/api/services/embeddings/enable", json={}, headers=infer)
        ).status_code == 403
        assert (
            await c.post("/v1/embeddings", json={"model": "mini", "input": ["hi"]}, headers=infer)
        ).status_code == 409
        assert (
            await c.post("/api/services/embeddings/enable", json={}, headers=admin)
        ).status_code == 200
        aux.memory._sample = lambda: 5
        assert (
            await c.post("/api/services/embeddings/load", json={"model": "mini"}, headers=admin)
        ).status_code == 503
        assert aux.services["embeddings"].process is None
        aux.memory._sample = lambda: 128 * 1024**3
        for name, model in (("embeddings", "mini"), ("transcription", "whisper")):
            assert (
                await c.post(f"/api/services/{name}/enable", json={}, headers=admin)
            ).status_code == 200
            assert (
                await c.post(f"/api/services/{name}/load", json={"model": model}, headers=admin)
            ).status_code == 200
        result = await c.post(
            "/v1/embeddings", json={"model": "mini", "input": ["one", "two"]}, headers=infer
        )
        assert result.status_code == 200
        assert [item["index"] for item in result.json()["data"]] == [0, 1]
        encoded = await c.post(
            "/v1/embeddings",
            json={"model": "mini", "input": "one", "encoding_format": "base64"},
            headers=infer,
        )
        assert struct.unpack(
            "<2f", base64.b64decode(encoded.json()["data"][0]["embedding"])
        ) == pytest.approx((0.1, 0.2))
        # Loaded services no longer block chat lifecycle: with no engine selected the chat
        # path fails for its own reason, not because an auxiliary service is loaded.
        with pytest.raises(HTTPException) as chat_error:
            await app.state.manager._load_model("other")
        assert chat_error.value.detail == "Select an engine before a model"
        # A request while a chat model is loaded is admitted by live memory alone; the old
        # unconfigured "chat growth budget" refusal is gone.
        app.state.manager.phase = "ready"
        assert (await aux.infer("embeddings", "mini", {"input": ["hello"]}))["vectors"]
        app.state.manager.phase = "unloaded"
        pending = asyncio.create_task(aux.infer("embeddings", "mini", {"input": ["wait"]}))
        while not aux.services["embeddings"].busy:
            await asyncio.sleep(0)
        assert (
            await c.post("/api/services/embeddings/disable", json={}, headers=admin)
        ).status_code == 409
        transcript = await aux.infer("transcription", "whisper", {"path": "fixture"})
        assert transcript["text"] == "hello"
        assert (await pending)["vectors"] == [[0.1, 0.2]]
        audio = io.BytesIO()
        with wave.open(audio, "wb") as wav:
            wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            wav.writeframes(b"\0\0" * 16000)
        result = await c.post(
            "/v1/audio/transcriptions",
            headers=infer,
            data={
                "model": "whisper",
                "response_format": "verbose_json",
                "timestamp_granularities[]": "word",
            },
            files={"file": ("audio.wav", audio.getvalue(), "audio/wav")},
        )
        assert result.status_code == 200, result.text
        assert result.json()["words"][0]["word"] == "hello"
        assert not list(tmp_path.glob("audio-*"))
        aux.memory._sample = lambda: 5
        result = await c.post(
            "/v1/embeddings", json={"model": "mini", "input": "hi"}, headers=infer
        )
        assert result.status_code == 503
        assert (
            await c.post("/api/services/embeddings/unload", json={}, headers=admin)
        ).status_code == 200
    await aux.close()
    await app.state.manager.close()
