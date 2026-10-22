import httpx
import pytest


@pytest.mark.asyncio
async def test_load_and_delegation_keep_explicit_selection_and_separate_credentials():
    from gateway.client import GatewayClient

    requests = []

    def backend(request):
        import json

        requests.append((request.url.path, request.headers, json.loads(request.content or b"{}")))
        if request.url.path == "/api/model":
            return httpx.Response(200, json={"phase": "ready", "selection_id": "selection-1"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "Worker answer"}}]})

    async with GatewayClient(
        "http://gateway.lan:9000", "a" * 32, "b" * 32, transport=httpx.MockTransport(backend)
    ) as client:
        state = await client.load("llama", "model", context=65536, reasoning="off")
        result = await client.delegate(
            "model", "Review this code", state["selection_id"], context="print(1)", max_tokens=2048
        )
    assert result["content"] == "Worker answer"
    assert requests[0][0] == "/api/model"
    assert requests[0][1]["authorization"] == "Bearer " + "a" * 32
    assert requests[0][2] == {
        "engine": "llama",
        "model": "model",
        "context": 65536,
        "reasoning": "off",
        "force": False,
    }
    assert requests[1][0] == "/v1/chat/completions"
    assert requests[1][1]["authorization"] == "Bearer " + "b" * 32
    assert requests[1][1]["x-igm-selection-id"] == "selection-1"
    assert requests[1][2]["max_tokens"] == 2048
    assert requests[1][2]["stream"] is False
    assert "print(1)" in requests[1][2]["messages"][-1]["content"]


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/a",
        "http://user:pass@host",
        "http://host/v1",
        "http://host?key=secret",
        "http://host/#x",
        "http://host:0",
    ],
)
def test_remote_client_rejects_non_origin_urls(url):
    from gateway.client import GatewayClient

    with pytest.raises(ValueError):
        GatewayClient(url, "a" * 32)


@pytest.mark.asyncio
async def test_delegation_never_loads_or_retries_busy_or_redirected_gateway():
    from gateway.client import GatewayClient, GatewayError

    calls = []

    def backend(request):
        calls.append(request.url.path)
        return httpx.Response(409, json={"detail": "Engine already processing"})

    async with GatewayClient(
        "http://gateway.lan", "a" * 32, transport=httpx.MockTransport(backend)
    ) as client:
        with pytest.raises(GatewayError, match="409"):
            await client.delegate("model", "Task", "selection")
    assert calls == ["/v1/chat/completions"]
    calls.clear()

    def redirect(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://elsewhere/api/status"})

    async with GatewayClient(
        "http://gateway.lan", "a" * 32, transport=httpx.MockTransport(redirect)
    ) as client:
        with pytest.raises(GatewayError):
            await client.status()
    assert calls == ["http://gateway.lan/api/status"]


@pytest.mark.asyncio
@pytest.mark.parametrize("tokens", [0, -1, True, 16385])
async def test_delegation_rejects_unbounded_output_before_network(tokens):
    from gateway.client import GatewayClient

    async with GatewayClient("http://gateway.lan", "a" * 32) as client:
        with pytest.raises(ValueError):
            await client.delegate("model", "Task", "selection", max_tokens=tokens)


def test_client_fragments_pin_smaller_budget_and_only_reference_credentials():
    from gateway.client import client_fragment

    opencode = client_fragment("opencode", "http://gateway.lan:9000", "example", 65536, 4096)
    model = opencode["provider"]["igm"]["models"]["example"]
    assert model["limit"] == {"context": 65536, "output": 4096}
    assert opencode["provider"]["igm"]["options"]["apiKey"] == "{env:IGM_INFERENCE_KEY}"
    hermes = client_fragment("hermes", "http://gateway.lan:9000", "example", 65536, 4096)
    assert hermes["model"]["context_length"] == 65536
    assert hermes["providers"]["igm"]["key_env"] == "IGM_INFERENCE_KEY"
    assert "a" * 32 not in str(hermes)


@pytest.mark.asyncio
async def test_registration_inspects_before_saving_candidate():
    from gateway.client import GatewayClient

    candidate = {"engine": "llama", "profile": "gguf", "model": "new", "path": "/models/new.gguf"}
    calls = []

    def backend(request):
        import json

        calls.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"candidate": candidate, "summary": {}})

    async with GatewayClient(
        "http://gateway.lan", "a" * 32, transport=httpx.MockTransport(backend)
    ) as client:
        inspection = await client.inspect_model("llama", "/models/new.gguf", "gguf")
        await client.add_model(inspection["candidate"])
    assert calls == [
        ("/api/models/inspect", {"engine": "llama", "path": "/models/new.gguf", "profile": "gguf"}),
        ("/api/models", candidate),
    ]


@pytest.mark.asyncio
async def test_registration_preview_validates_all_requested_settings_at_gateway():
    import json

    from gateway.client import GatewayClient

    calls = []

    def backend(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"candidate": calls[-1]})

    async with GatewayClient(
        "http://gateway.lan", "a" * 32, transport=httpx.MockTransport(backend)
    ) as client:
        await client.inspect_model(
            "llama", "/models/model.gguf", "gguf", model="reviewed", context=65536, reasoning="off"
        )
    assert calls == [
        {
            "engine": "llama",
            "path": "/models/model.gguf",
            "profile": "gguf",
            "model": "reviewed",
            "context": 65536,
            "reasoning": "off",
        }
    ]


@pytest.mark.asyncio
async def test_registration_errors_explain_correction_and_redact_credentials():
    from gateway.client import GatewayClient, GatewayError

    def backend(request):
        return httpx.Response(
            422, json={"detail": "Required tokenizer companion is missing: " + "a" * 32}
        )

    async with GatewayClient(
        "http://gateway.lan", "a" * 32, transport=httpx.MockTransport(backend)
    ) as client:
        with pytest.raises(GatewayError) as error:
            await client.inspect_model("llama", "/models/example.gguf")
    assert "tokenizer companion is missing" in str(error.value)
    assert "a" * 32 not in str(error.value)
