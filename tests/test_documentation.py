"""Portable documentation, branding and API-contract checks; no live engine calls."""

import ast
import json
import re
import struct
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from gateway.app import create_app

ROOT = Path(__file__).resolve().parents[1]


def test_new_documentation_links_resolve():
    paths = [
        ROOT / "README.md",
        ROOT / "docs/clients.md",
        ROOT / "docs/orchestration.md",
        ROOT / "docs/mcp-registration.md",
        ROOT / "docs/branding.md",
        ROOT / "docs/lucebox-ds4a.md",
        ROOT / "skills/inference-gateway-manager/SKILL.md",
        ROOT / "skills/inference-gateway-manager/references/context-sizing.md",
    ]
    for path in paths:
        content = path.read_text()
        links = re.findall(r"\]\(([^)]+)\)", content)
        links += re.findall(r'(?:src|srcset)="([^"]+)"', content)
        for link in links:
            if link.startswith(("https://", "http://", "#")):
                continue
            target = link.split("#")[0]
            assert (path.parent / target).exists(), (path, link)
        assert "/home/" not in content
        assert re.search(r"192\.168\.\d+\.\d+", content) is None


def test_skill_frontmatter_and_safety_contract():
    content = (ROOT / "skills/inference-gateway-manager/SKILL.md").read_text()
    assert content.startswith("---\n")
    frontmatter, body = content[4:].split("\n---\n", 1)
    match = re.search(r"^description: (.+)$", frontmatter, re.M)
    assert match is not None
    description = match.group(1)
    assert len(description) <= 60 and description.endswith(".")
    for field in ("name", "version", "author", "license", "platforms", "metadata"):
        assert re.search(rf"^{field}:", frontmatter, re.M)
    for requirement in (
        "force:false",
        "explicit authority",
        "read back",
        "IGM_CONTROL_KEY",
        "IGM_INFERENCE_KEY",
        "no durable lease",
        "unknown native occupancy",
        "gateway_only",
    ):
        assert requirement.lower() in body.lower()


def test_logo_inputs_and_social_export():
    for name in ("logo-dark.svg", "logo-light.svg", "logo-mono.svg", "social-preview.svg"):
        tree = ET.fromstring((ROOT / "docs/assets" / name).read_text())
        circles = [node for node in tree.iter() if node.tag.endswith("circle")]
        assert len(circles) == 3
        assert circles[2].get("opacity") == ".24"
        assert not any(node.tag.endswith("script") for node in tree.iter())
    ET.parse(ROOT / "gateway/static/favicon.svg")
    png = (ROOT / "docs/assets/social-preview.png").read_bytes()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", png[16:24]) == (1280, 640)


def test_documented_smoke_test_executes_without_lifecycle_calls(monkeypatch, capsys):
    content = (ROOT / "docs/clients.md").read_text()
    match = re.search(r"```python\n(.*?)\n```", content, re.S)
    assert match is not None
    code = match.group(1)
    ast.parse(code)
    monkeypatch.setenv("IGM_BASE_URL", "http://fixture")
    monkeypatch.setenv("IGM_INFERENCE_KEY", "documentation-fixture-key")
    calls = []

    class Response:
        def __init__(self, value):
            self.value = json.dumps(value)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return self.value

    def urlopen(request, timeout):
        calls.append(request.full_url)
        assert request.get_header("Authorization") == "Bearer documentation-fixture-key"
        if request.full_url.endswith("/models"):
            return Response({"data": [{"id": "fixture-model", "status": "loaded"}]})
        payload = json.loads(request.data)
        assert payload["model"] == "fixture-model"
        assert payload["stream"] is False
        return Response({"choices": [{"message": {"content": "fixture reply"}}]})

    with patch("urllib.request.urlopen", urlopen):
        exec(compile(code, "docs/clients.md", "exec"), {})
    assert calls == ["http://fixture/v1/models", "http://fixture/v1/chat/completions"]
    assert capsys.readouterr().out == "fixture reply\n"


@pytest.mark.asyncio
async def test_documented_discovery_roles_and_favicon(tmp_path):
    app = create_app(
        {
            "allowed_hosts": ["testserver"],
            "engines": {
                "fixture": {
                    "label": "Fixture",
                    "models": {"fixture-model": {"enabled": True, "context": 4096}},
                }
            },
        },
        "admin-documentation-fixture-key-32",
        "inference-documentation-key-32!!!",
        tmp_path,
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            assert (await client.get("/v1")).status_code == 404
            assert (await client.get("/v1/models")).status_code == 401
            client.headers["Authorization"] = "Bearer inference-documentation-key-32!!!"
            models = (await client.get("/v1/models")).json()["data"]
            assert models[0]["status"] == "unloaded"
            assert (await client.get("/api/engines")).status_code == 403
            client.headers["Authorization"] = "Bearer admin-documentation-fixture-key-32"
            catalog = (await client.get("/api/engines")).json()["engines"]
            assert catalog[0]["id"] == "fixture"
            assert catalog[0]["models"][0]["id"] == "fixture-model"
            status = (await client.get("/api/status")).json()
            assert status["loaded_model"] is None
            assert status["active_requests"] == 0
            icon = await client.get("/assets/favicon.svg")
            assert icon.status_code == 200
            assert icon.content == (ROOT / "gateway/static/favicon.svg").read_bytes()
            assert "/assets/favicon.svg" in (await client.get("/")).text
    finally:
        await app.state.manager.close()
