import subprocess
import sys

import pytest


def test_remove_cli_uses_authenticated_registry_api_without_touching_files(monkeypatch, capsys):
    import json

    import httpx

    from gateway import __main__
    from gateway.client import GatewayClient

    calls = []

    def backend(request):
        calls.append((request.method, request.url.path, request.headers["authorization"]))
        return httpx.Response(200, json={"removed": True})

    monkeypatch.setattr(
        "gateway.client.GatewayClient",
        lambda url, key: GatewayClient(
            url,
            key,
            transport=httpx.MockTransport(backend),
        ),
    )
    monkeypatch.setenv("IGM_CONTROL_KEY", "a" * 32)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "inference-gateway",
            "models",
            "remove",
            "--url",
            "http://gateway.lan",
            "--engine",
            "llama",
            "--model",
            "saved",
        ],
    )
    __main__.main()
    assert json.loads(capsys.readouterr().out) == {"removed": True}
    assert calls == [("DELETE", "/api/models/llama/saved", "Bearer " + "a" * 32)]


def test_password_cli_prompts_and_invalidates_existing_sessions(tmp_path, monkeypatch, capsys):
    from gateway import __main__
    from gateway.browser_auth import BrowserAuth

    store = BrowserAuth(tmp_path, {"enabled": True})
    store.set_password("the initial sufficiently long password")
    token, _ = store.login("the initial sufficiently long password", True, "a", "Browser")
    monkeypatch.setattr(
        sys, "argv", ["inference-gateway", "set-password", "--state-dir", str(tmp_path)]
    )
    monkeypatch.setattr("getpass.getpass", lambda _: "a newly changed long password")
    __main__.main()
    assert store.authenticate(token) is None
    assert store.login("a newly changed long password", False, "b", "Browser")[0]
    assert "a newly changed long password" not in capsys.readouterr().out


def test_client_config_cli_uses_live_capacity_and_never_changes_it(monkeypatch, capsys):
    import json

    from gateway import __main__
    from gateway.client import GatewayClient

    calls = []

    async def catalog(self):
        calls.append("catalog")
        return {"engines": [{"id": "llama", "models": [{"id": "model", "context": 262144}]}]}

    async def status(self):
        calls.append("status")
        return {"phase": "ready", "loaded_model": "model", "active_settings": {"context": 131072}}

    monkeypatch.setattr(GatewayClient, "catalog", catalog)
    monkeypatch.setattr(GatewayClient, "status", status)
    monkeypatch.setenv("IGM_CONTROL_KEY", "a" * 32)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "inference-gateway",
            "client-config",
            "--url",
            "http://host:9000",
            "--client",
            "opencode",
            "--model",
            "model",
            "--context",
            "65536",
        ],
    )
    __main__.main()
    result = json.loads(capsys.readouterr().out)
    assert result["provider"]["igm"]["models"]["model"]["limit"]["context"] == 65536
    assert calls == ["catalog", "status"]
    calls.clear()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "inference-gateway",
            "client-config",
            "--url",
            "http://host:9000",
            "--client",
            "hermes",
            "--model",
            "model",
            "--context",
            "262144",
        ],
    )
    with pytest.raises(SystemExit):
        __main__.main()
    assert calls == ["catalog", "status"]


def test_cli_creates_private_keys_without_overwriting(tmp_path):
    output = tmp_path / "keys.env"
    cmd = [sys.executable, "-m", "gateway", "init-keys", "--output", str(output)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert output.stat().st_mode & 0o777 == 0o600
    before = output.read_bytes()
    assert b"IGM_CONTROL_KEY=" in before and b"IGM_INFERENCE_KEY=" in before
    assert subprocess.run(cmd, capture_output=True).returncode != 0
    assert output.read_bytes() == before
    assert before.decode() not in result.stdout
