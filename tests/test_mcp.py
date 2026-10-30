"""Real SDK discovery and tool calls, including the stdio subprocess transport."""

import json
import os
import sys

import httpx
import pytest


@pytest.mark.asyncio
async def test_mcp_tools_only_offer_normal_control_and_bounded_delegation():
    from gateway.client import GatewayClient
    from gateway.mcp import create_server

    def backend(request):
        if request.url.path == "/api/status":
            return httpx.Response(200, json={"phase": "ready", "selection_id": "one"})
        return httpx.Response(409, json={"detail": "busy"})

    async with GatewayClient(
        "http://gateway.lan", "a" * 32, transport=httpx.MockTransport(backend)
    ) as client:
        server = create_server(client)
        tools = await server.list_tools()
        assert {t.name for t in tools} == {
            "gateway_catalog",
            "gateway_status",
            "gateway_telemetry",
            "gateway_load",
            "gateway_unload",
            "gateway_delegate",
        }
        for tool in tools:
            assert "force" not in tool.inputSchema.get("properties", {})
            assert "url" not in tool.inputSchema.get("properties", {})
        result = await server.call_tool("gateway_status", {})
        assert "ready" in str(result)
        with pytest.raises(Exception, match="409"):
            await server.call_tool(
                "gateway_delegate", {"model": "example", "prompt": "Task", "selection_id": "one"}
            )


@pytest.mark.asyncio
async def test_real_stdio_exchange_lists_tools_and_reports_connection_failure_safely():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "gateway.mcp", "--url", "http://127.0.0.1:1"],
        env={**os.environ, "IGM_CONTROL_KEY": "test-control-" + "s" * 40},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert len(tools.tools) == 6
            result = await session.call_tool("gateway_status", {})
            assert result.isError
            rendered = json.dumps(result.model_dump(mode="json"))
            assert "connection failed" in rendered.lower()
            assert "test-control-" not in rendered


def test_mcp_entrypoint_imports_without_linux_process_modules():
    import subprocess

    code = (
        "import sys; sys.modules['fcntl']=None; sys.modules['gateway.core']=None; "
        "import gateway.mcp; import gateway.__main__; print('portable')"
    )
    result = subprocess.run([sys.executable, "-c", code], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "portable"


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "linux", reason="The fixture gateway host requires Linux")
async def test_stdio_adapter_reaches_a_separate_gateway_process(tmp_path):
    import socket
    import subprocess
    import time

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    code = """
import sys, uvicorn
from gateway.app import create_app
app=create_app({'allowed_hosts':['127.0.0.1'],'engines':{'fixture':{'models':{}}}},
               'a'*32, 'b'*32, sys.argv[2])
uvicorn.run(app, host='127.0.0.1',port=int(sys.argv[1]),log_level='error',access_log=False)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(port), str(tmp_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                if httpx.get(url + "/health", timeout=0.2, trust_env=False).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.03)
        else:
            pytest.fail("Disposable gateway did not become ready")
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "gateway.mcp", "--url", url],
            env={**os.environ, "IGM_CONTROL_KEY": "a" * 32},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("gateway_status", {})
                assert not result.isError
                data = json.loads(result.content[0].text)
                assert data["phase"] == "unloaded" and data["loaded_model"] is None
                result = await session.call_tool("gateway_catalog", {})
                assert json.loads(result.content[0].text)["engines"][0]["id"] == "fixture"
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@pytest.mark.asyncio
async def test_mcp_load_exposes_and_forwards_selection_system_prompt():
    from gateway.client import GatewayClient
    from gateway.mcp import create_server

    requests = []

    def backend(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"selection_id": "selected"})

    async with GatewayClient(
        "http://gateway.lan", "a" * 32, transport=httpx.MockTransport(backend)
    ) as client:
        server = create_server(client)
        load = next(tool for tool in await server.list_tools() if tool.name == "gateway_load")
        assert "system_prompt" in load.inputSchema["properties"]
        assert "system_prompt" not in load.inputSchema.get("required", [])
        for extra in ({"system_prompt": "Be concise."}, {}, {"system_prompt": ""}):
            await server.call_tool("gateway_load", {"engine": "one", "model": "fixture", **extra})
    assert requests == [
        {"engine": "one", "model": "fixture", "force": False, "system_prompt": "Be concise."},
        {"engine": "one", "model": "fixture", "force": False},
        {"engine": "one", "model": "fixture", "force": False, "system_prompt": ""},
    ]
