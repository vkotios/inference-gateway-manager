import asyncio
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from gateway.app import create_app


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code,expected", [(0, 202), (1, 503)])
async def test_recovery_scheduling_and_duplicate_refusal(
    monkeypatch, tmp_path, exit_code, expected
):
    calls = []

    class Process:
        returncode = exit_code

        async def wait(self):
            return self.returncode

    async def execute(*argv, **kwargs):
        calls.append((argv, kwargs))
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", execute)
    app = create_app(
        {
            "engines": {"one": {"models": {}}},
            "allowed_hosts": ["testserver"],
            "recovery": {"restart_gateway": True},
        },
        "a" * 32,
        "b" * 32,
        tmp_path,
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": "Bearer " + "a" * 32},
        ) as client:
            r = await client.post(
                "/api/recovery/restart-gateway", json={"confirmation": "restart-gateway"}
            )
            assert r.status_code == expected
            assert calls[0][0] == (
                "/usr/bin/sudo",
                "-n",
                "/usr/local/libexec/igm-recovery",
                "restart-gateway",
            )
            assert set(calls[0][1]["env"]) == {"PATH", "LANG"}
            capabilities = (await client.get("/api/recovery")).json()
            assert capabilities["pending"] is (exit_code == 0)
            if exit_code == 0:
                assert r.json()["status"] == "scheduled"
                assert (
                    await client.post(
                        "/api/recovery/restart-gateway", json={"confirmation": "restart-gateway"}
                    )
                ).status_code == 409
                assert len(calls) == 1
    finally:
        await app.state.manager.close()


@pytest.mark.parametrize(
    "options", [None, "yes", {"restart_gateway": "true"}, {"reboot_host": 1}, {"command": ["evil"]}]
)
def test_recovery_config_rejects_nonboolean_or_arbitrary_commands(options):
    from gateway.config import validate

    with pytest.raises(ValueError, match="recovery"):
        validate({"engines": {"one": {"models": {}}}, "recovery": options})


def test_dashboard_recovery_confirmation_and_duplicate_guard():
    script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const elements={};let allowed=false,calls=[];
const ctx=vm.createContext({document:{getElementById:id=>elements[id]??=( {value:'',textContent:'',disabled:false} )},location:{origin:'http://testserver'},confirm:()=>allowed});
vm.runInContext(fs.readFileSync('gateway/static/app.js','utf8'),ctx);
vm.runInContext("key='synthetic'; recovery={restart_gateway:true,reboot_host:false,pending:false};",ctx);
ctx.record=(path,body)=>calls.push({path,body});
vm.runInContext('api=async(path,body)=>record(path,body)',ctx);
(async()=>{
 await vm.runInContext("recover('restart-gateway')",ctx);
 assert.equal(calls.length,0);
 allowed=true;
 await vm.runInContext("recover('restart-gateway')",ctx);
 assert.equal(calls.length,1);
 assert.equal(calls[0].path,'/api/recovery/restart-gateway');
 assert.equal(calls[0].body.confirmation,'restart-gateway');
 await vm.runInContext("recover('restart-gateway')",ctx);
 assert.equal(calls.length,1);
 assert.equal(elements['reboot-host'].disabled,true);
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_recovery_dashboard_has_distinct_confirmed_controls():
    markup = Path("gateway/static/index.html").read_text()
    script = Path("gateway/static/app.js").read_text()
    assert 'id="restart-gateway"' in markup
    assert 'id="reboot-host"' in markup
    assert "confirmation:action" in script
    assert "confirm(" in script


def test_privileged_helper_schedules_only_fixed_action(monkeypatch):
    path = Path("examples/igm-recovery-helper.py")
    assert path.exists(), "Privileged recovery helper missing"
    spec = importlib.util.spec_from_file_location("recovery_helper", path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    calls = []
    monkeypatch.setattr(helper.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        helper.subprocess,
        "run",
        lambda argv, **kw: calls.append((argv, kw)) or SimpleNamespace(returncode=0),
    )
    assert helper.main(["restart-gateway"]) == 0
    assert calls[0][0][-3:] == [
        "/usr/bin/systemctl",
        "restart",
        "inference-gateway-manager.service",
    ]
    assert "--on-active=3s" in calls[0][0]
    assert "--collect" in calls[0][0]
    calls.clear()
    assert helper.main(["reboot-host"]) == 0
    assert calls[0][0][-2:] == ["/usr/bin/systemctl", "reboot"]
    calls.clear()
    assert helper.main(["restart-gateway", ";evil"]) != 0
    assert helper.main(["evil"]) != 0
    assert not calls
    monkeypatch.setattr(helper.os, "geteuid", lambda: 1000)
    assert helper.main(["restart-gateway"]) != 0
    assert not calls


def test_recovery_cli_requires_confirmation_before_http(monkeypatch):
    from gateway.__main__ import main

    monkeypatch.setattr(
        sys, "argv", ["gateway", "recovery", "reboot-host", "--url", "http://127.0.0.1:9000"]
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2


def test_recovery_cli_posts_exact_confirmed_action(monkeypatch, capsys):
    from gateway.__main__ import main

    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(202, json={"status": "scheduled"}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setenv("IGM_CONTROL_KEY", "a" * 32)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gateway",
            "recovery",
            "restart-gateway",
            "--url",
            "http://127.0.0.1:9000",
            "--confirm",
            "restart-gateway",
        ],
    )
    main()
    assert calls[0][0] == "http://127.0.0.1:9000/api/recovery/restart-gateway"
    assert calls[0][1]["json"] == {"confirmation": "restart-gateway"}
    assert calls[0][1]["follow_redirects"] is False
    assert "a" * 32 not in capsys.readouterr().out


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,key,body,enabled,expected",
    [
        ("restart-gateway", "a", {"confirmation": "restart-gateway"}, False, 403),
        ("reboot-host", "a", {"confirmation": "reboot-host"}, False, 403),
        ("restart-gateway", "b", {"confirmation": "restart-gateway"}, True, 403),
        ("reboot-host", "b", {"confirmation": "reboot-host"}, True, 403),
        ("restart-gateway", "a", {"confirmation": "wrong"}, True, 400),
        ("reboot-host", "a", {"confirmation": "wrong"}, True, 400),
        ("restart-gateway", "a", {"confirmation": "restart-gateway", "argv": ["evil"]}, True, 422),
    ],
)
async def test_recovery_refusal_never_executes(
    monkeypatch, tmp_path, action, key, body, enabled, expected
):
    config = {
        "engines": {"one": {"models": {}}},
        "allowed_hosts": ["testserver"],
        "recovery": {"restart_gateway": enabled, "reboot_host": enabled},
    }
    app = create_app(config, "a" * 32, "b" * 32, tmp_path)

    async def forbidden(*args, **kwargs):
        raise AssertionError("Refused request reached process execution")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            r = await client.post(
                "/api/recovery/" + action,
                json=body,
                headers={"Authorization": "Bearer " + key * 32},
            )
            assert r.status_code == expected
    finally:
        await app.state.manager.close()
