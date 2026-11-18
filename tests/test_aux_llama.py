import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from gateway.aux_llama import _embed
from gateway.auxiliary import GGUF_REQUEST_LIMIT, Auxiliary
from gateway.config import validate
from gateway.footprints import aux_fingerprint


def test_gguf_config_is_scoped_to_embeddings_and_settings_invalidate_footprints(tmp_path):
    spec = {
        "backend": "llama.cpp",
        "path": str(tmp_path / "model.gguf"),
        "runner": str(tmp_path / "llama-server"),
        "context": 2048,
        "threads": 8,
        "repack": False,
        "load_bytes": 3 * 1024**3,
        "request_bytes": 512 * 1024**2,
    }

    def cfg(kind, model_spec):
        return {
            "engines": {"one": {"models": {}}},
            "auxiliary": {kind: {"models": {"zembed": model_spec}}},
        }

    validate(cfg("embeddings", spec))
    for invalid in (dict(spec, runner="relative"), dict(spec, repack=1), dict(spec, context=True)):
        with pytest.raises(ValueError):
            validate(cfg("embeddings", invalid))
    with pytest.raises(ValueError):
        validate(cfg("transcription", spec))

    initial = aux_fingerprint("embeddings", "zembed", spec)
    assert aux_fingerprint("embeddings", "zembed", dict(spec, repack=True)) != initial
    assert aux_fingerprint("embeddings", "zembed", dict(spec, context=4096)) != initial


async def test_batches_are_bounded_and_return_vectors_in_input_order():
    seen = []

    def handler(request):
        body = json.loads(request.content)
        if request.url.path == "/tokenize":
            assert body["parse_special"] and not body["add_special"]
            return httpx.Response(200, json={"tokens": list(map(int, body["content"].split()))})
        seen.append(body["input"])
        items = [{"index": i, "embedding": [tokens[0]]} for i, tokens in enumerate(body["input"])]
        return httpx.Response(200, json={"data": list(reversed(items))})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://llama"
    ) as http:
        result = await _embed(http, ["1"] * 9 + ["2 2 2 2", "3 3 3 3"], 8)
        assert [len(batch) for batch in seen] == [8, 2, 1]
        assert result == {"vectors": [[1]] * 9 + [[2], [3]], "tokens": 17}
        with pytest.raises(HTTPException) as raised:
            await _embed(http, ["1", "1 " * 8], 8)
        assert raised.value.status_code == 400
        assert len(seen) == 3


async def test_gguf_requests_queue_serially_with_bounded_capacity(tmp_path, monkeypatch):
    model = "gguf"
    aux = Auxiliary(
        SimpleNamespace(state_dir=tmp_path),
        {"embeddings": {"models": {model: {"backend": "llama.cpp"}}}},
    )
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    arrived = []

    async def infer_one(_service, _model, value):
        arrived.append(value)
        if value == 0:
            first_started.set()
            await release_first.wait()
        return value

    monkeypatch.setattr(aux, "_infer", infer_one)
    tasks = [asyncio.create_task(aux.infer("embeddings", model, 0))]
    await first_started.wait()
    tasks.extend(
        asyncio.create_task(aux.infer("embeddings", model, i)) for i in range(1, GGUF_REQUEST_LIMIT)
    )
    try:
        await asyncio.sleep(0)
        assert arrived == [0]
        with pytest.raises(HTTPException) as raised:
            await aux.infer("embeddings", model, GGUF_REQUEST_LIMIT)
        assert raised.value.status_code == 429
        assert raised.value.headers["Retry-After"] == "1"
        tasks[1].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[1]
    finally:
        release_first.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
    assert isinstance(results.pop(1), asyncio.CancelledError)
    assert results == arrived == [0, *range(2, GGUF_REQUEST_LIMIT)]
    assert aux.get("embeddings").llama_requests == 0
