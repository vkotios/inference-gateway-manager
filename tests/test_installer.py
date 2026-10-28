"""Fresh onboarding safety and scripted installs with fake pip/service commands."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from gateway.config import validate
from installer.generate_config import ENGINE_IDS, _rewrite, build_config, main, write_config
from installer.setup_files import check_target, copy_public_files, render_unit

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "installer/install.sh"


def test_all_engine_choices_follow_public_catalog_and_preserve_profiles():
    example = json.loads((ROOT / "examples/config.example.json").read_text())
    assert (
        set(ENGINE_IDS)
        == set(example["engines"])
        == {
            "llama",
            "strata",
            "kyojin",
            "dwarfstar",
            "gufo",
            "lucebox",
            "ds4a",
        }
    )
    config = build_config(ENGINE_IDS, "/data/models")
    validate(config)
    assert "/opt/models" not in json.dumps(config)
    assert "default" not in config
    for engine in config["engines"].values():
        assert all(spec["enabled"] is False for spec in engine["models"].values())
    luce = config["engines"]["lucebox"]["profiles"]["gguf"]
    ds4a = config["engines"]["ds4a"]["profiles"]["dspark-gguf"]
    assert luce["env"]["LUCE_DS4_SPEC"] == "0" and "--draft" not in luce["argv"]
    assert ds4a["artifacts"] == {"draft": {"kind": "file", "format": "gguf"}}
    assert "{artifact.draft}" in ds4a["argv"]
    assert ds4a["context_limits"]["presets"] == [4096, 8192, 16384, 32768, 65536, 131072]
    assert config["engines"]["dwarfstar"]["profiles"]["gguf"]["argv"][0] == "/opt/ds4/ds4-server"


@pytest.mark.parametrize("engine", ENGINE_IDS)
def test_single_selection_retains_all_conflict_matchers(engine):
    cfg = build_config([engine], "/srv/models")
    assert list(cfg["engines"]) == [engine]
    assert (
        cfg["native_matchers"]
        == json.loads((ROOT / "examples/config.example.json").read_text())["native_matchers"]
    )
    assert cfg["recovery"] == {"restart_gateway": False, "reboot_host": False}
    assert "mcp" not in cfg


def test_model_path_rewrite_respects_directory_boundaries():
    value = ["/opt/models", "/opt/models/a.gguf", "/opt/models-old/a.gguf", "{artifact.draft}"]
    assert _rewrite(value, Path("/data/models")) == [
        "/data/models",
        "/data/models/a.gguf",
        "/opt/models-old/a.gguf",
        "{artifact.draft}",
    ]


@pytest.mark.parametrize(
    "engines,models,options",
    [
        ([], "/data/models", {}),
        (["missing"], "/data/models", {}),
        (["ds4a"], "relative", {}),
        (["ds4a"], "/models", {"host": "0.0.0.0"}),
        (["ds4a"], "/models", {"host": "8.8.8.8"}),
        (["ds4a"], "/models", {"host": "192.168.50.2"}),
        (["ds4a"], "/models", {"host": "192.168.50.2", "allowed_network": "192.168.60.0/24"}),
        (["ds4a"], "/models", {"host": "192.168.50.2", "allowed_network": "0.0.0.0/0"}),
        (["ds4a"], "/models", {"allowed_hosts": ["*"]}),
        (["ds4a"], "/models", {"allowed_hosts": ["http://gateway:9000"]}),
    ],
)
def test_bad_onboarding_values_are_rejected(engines, models, options):
    with pytest.raises(ValueError):
        build_config(engines, models, **options)


def test_lan_config_binds_one_ip_and_exact_host():
    cfg = build_config(
        ["lucebox", "ds4a"],
        "/models",
        host="192.168.50.2",
        allowed_network="192.168.50.0/24",
        allowed_hosts=["gateway.lan"],
    )
    assert cfg["allowed_hosts"] == ["192.168.50.2", "gateway.lan"]
    assert cfg["allowed_networks"] == ["192.168.50.0/24"]
    ipv6 = build_config(["ds4a"], "/models", host="fd12::2", allowed_network="fd12::/64")
    assert ipv6["allowed_hosts"] == ["fd12::2"]
    assert cfg["engines"]["lucebox"]["profiles"]["gguf"]["base_url"] == "http://127.0.0.1:8084"


def test_cli_overwrite_refusal_and_idempotence(tmp_path):
    output = tmp_path / "config.local.json"
    args = ["--engine", "ds4a", "--models-dir", str(tmp_path / "models"), "--output", str(output)]
    assert main(args) == 0
    before = output.stat().st_mtime_ns
    assert main(args) == 0 and output.stat().st_mtime_ns == before
    assert main(args + ["--restart-gateway"]) == 1
    assert json.loads(output.read_text())["recovery"]["restart_gateway"] is False
    link = tmp_path / "link.json"
    link.symlink_to(tmp_path / "absent.json")
    with pytest.raises(ValueError):
        write_config({}, link)
    assert not (tmp_path / "absent.json").exists()


def test_list_engines_needs_no_installed_gateway_dependencies():
    result = subprocess.run(
        [sys.executable, "-S", str(ROOT / "installer/generate_config.py"), "--list-engines"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == list(ENGINE_IDS)


@pytest.mark.parametrize("name", ["config.local.json", ".local", ".venv"])
def test_existing_install_is_refused_without_modification(tmp_path, name):
    source = tmp_path / "source"
    source.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    marker = target / name
    marker.write_text("existing private data")
    with pytest.raises(ValueError, match="Existing installation"):
        copy_public_files(source, target)
    assert marker.read_text() == "existing private data"


def test_copy_allowlist_excludes_host_files_and_follows_no_symlinks(tmp_path):
    from installer.setup_files import PUBLIC_DIRS, PUBLIC_FILES

    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    for name in PUBLIC_FILES:
        (source / name).write_text("public")
    for name in PUBLIC_DIRS:
        (source / name).mkdir()
        (source / name / "public.txt").write_text("public")
    (source / "config.local.json").write_text("secret")
    (source / ".local").mkdir()
    (source / ".local/credentials.env").write_text("secret")
    (source / "examples/.env").write_text("secret")
    (source / "examples/private.local.json").write_text("secret")
    (source / "examples/leak").symlink_to(source / ".local/credentials.env")
    copy_public_files(source, target)
    assert not (target / "config.local.json").exists() and not (target / ".local").exists()
    assert sorted(p.name for p in (target / "examples").iterdir()) == ["public.txt"]
    with pytest.raises(ValueError):
        check_target(source, source / "nested")


@pytest.mark.parametrize("system", [False, True])
def test_rendered_service_escapes_paths_and_uses_actual_account(tmp_path, system):
    import pwd

    install = tmp_path / "install with spaces & percent%"
    content = render_unit(ROOT, install, system)
    assert 'WorkingDirectory="' in content and "percent%%" in content
    assert f'"{install}/.venv/bin/python"'.replace("percent%", "percent%%") in content
    if system:
        assert f"User={pwd.getpwuid(os.getuid()).pw_name}" in content
        assert "WantedBy=multi-user.target" in content and "WatchdogSec=30" in content
    else:
        assert "User=" not in content


def shell_environment(tmp_path):
    """Only venv/pip and systemctl are faked; config/copy/credentials run for real."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    wrapper = fake_bin / "python3"
    wrapper.write_text(f"""#!{sys.executable}
import os, pathlib, sys
if sys.argv[1:3] == ['-m','venv']:
    folder=pathlib.Path(sys.argv[3])/'bin'
    folder.mkdir(parents=True)
    python=folder/'python'
    python.write_text({("#!" + sys.executable + chr(10) + "import os, sys" + chr(10) + 'if sys.argv[1:3] == ["-m", "pip"]: raise SystemExit(0)' + chr(10) + "os.execv(" + repr(sys.executable) + ", [" + repr(sys.executable) + "] + sys.argv[1:])" + chr(10))!r})
    python.chmod(0o755)
else:
    os.execv({sys.executable!r}, [{sys.executable!r}]+sys.argv[1:])
""")
    wrapper.chmod(0o755)
    command = fake_bin / "systemctl"
    command.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$IGM_TEST_SERVICE_LOG"\n')
    command.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    return {
        **os.environ,
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
        "PYTHONPATH": str(ROOT),
        "IGM_TEST_SERVICE_LOG": str(tmp_path / "service.log"),
    }


@pytest.mark.skipif(os.getuid() == 0, reason="Installer deliberately refuses root")
@pytest.mark.parametrize("mode", ["none", "user", "system"])
def test_scripted_onboarding_safely_configures_new_engines(tmp_path, mode):
    env = shell_environment(tmp_path)
    target = tmp_path / "installed gateway"
    models = tmp_path / "models"
    answers = [
        str(target),
        "lucebox,ds4a,dwarfstar",
        str(models),
        "yes",
        "192.168.50.2",
        "192.168.50.0/24",
        "gateway.lan",
        mode,
    ]
    if mode == "system":
        answers += ["yes", "no"]
    answers += ["yes"]
    result = subprocess.run(
        ["bash", str(INSTALLER)],
        input="\n".join(answers) + "\n",
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    config = validate(json.loads((target / "config.local.json").read_text()))
    assert set(config["engines"]) == {"lucebox", "ds4a", "dwarfstar"}
    assert config["allowed_hosts"] == ["192.168.50.2", "gateway.lan"]
    assert config["recovery"]["restart_gateway"] is (mode == "system")
    keys = target / ".local/credentials.env"
    assert keys.stat().st_mode & 0o777 == 0o600
    assert "IGM_CONTROL_KEY=" in keys.read_text()
    assert keys.read_text().strip() not in result.stdout
    assert "http://192.168.50.2:9000/v1" in result.stdout
    if mode == "system":
        assert "sudo install" in result.stdout
        assert not (tmp_path / "service.log").exists()
    elif mode == "user":
        actions = (tmp_path / "service.log").read_text()
        assert "enable inference-gateway-manager.service" in actions
        assert "start inference-gateway-manager.service" not in actions
    else:
        assert not (tmp_path / "service.log").exists()


@pytest.mark.skipif(os.getuid() == 0, reason="Installer deliberately refuses root")
@pytest.mark.parametrize(
    "answers",
    [
        "",
        "{target}\nall\n{models}\nno\nnone\nno\n",
        "{target}\nall\n{models}\nyes\n0.0.0.0\n0.0.0.0/0\n\nnone\nyes\n",
    ],
)
def test_cancel_eof_or_invalid_network_leaves_target_untouched(tmp_path, answers):
    env = shell_environment(tmp_path)
    target, models = tmp_path / "target", tmp_path / "models"
    result = subprocess.run(
        ["bash", str(INSTALLER)],
        input=answers.format(target=target, models=models),
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert not target.exists() and not models.exists(), result.stdout + result.stderr


def test_shell_syntax():
    if not shutil.which("bash"):
        pytest.skip("bash not installed")
    subprocess.run(["bash", "-n", str(INSTALLER)], check=True)


def test_installation_guide_local_links_resolve():
    import re

    page = ROOT / "docs/installation.md"
    for link in re.findall(r"\]\(([^)]+)\)", page.read_text()):
        if not link.startswith(("https://", "http://", "#")):
            assert (page.parent / link.split("#")[0]).exists(), link


@pytest.mark.skipif(os.getuid() == 0, reason="Installer deliberately refuses root")
def test_shell_refuses_existing_install_before_copying_or_installing(tmp_path):
    env = shell_environment(tmp_path)
    target = tmp_path / "existing"
    (target / ".local").mkdir(parents=True)
    keys = target / ".local/credentials.env"
    keys.write_text("existing keys")
    result = subprocess.run(
        ["bash", str(INSTALLER)],
        input=str(target) + "\n",
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode != 0 and "Existing installation" in result.stderr
    assert keys.read_text() == "existing keys"
    assert not (target / ".venv").exists()


@pytest.mark.skipif(os.getuid() == 0, reason="Installer deliberately refuses root")
def test_shell_all_selection_matches_public_catalog(tmp_path):
    env = shell_environment(tmp_path)
    target = tmp_path / "all-engines"
    answers = [str(target), "all", str(tmp_path / "models"), "no", "none", "yes"]
    result = subprocess.run(
        ["bash", str(INSTALLER)],
        input="\n".join(answers) + "\n",
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    config = json.loads((target / "config.local.json").read_text())
    assert tuple(config["engines"]) == ENGINE_IDS
