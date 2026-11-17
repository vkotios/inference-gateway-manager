import json

import httpx
import pytest
from fastapi import HTTPException

from gateway.aux_llama import _embed
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
