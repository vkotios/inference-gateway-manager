"""Installer tests: config builder correctness, shell syntax, docs coverage."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from gateway.config import validate
from installer.generate_config import ENGINE_IDS, build_config

ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "installer" / "generate_config.py"
INSTALLER = ROOT / "installer" / "install.sh"


def test_build_config_keeps_selected_engines_and_model_root():
    config = build_config(
        ["llama", "dwarfstar"],
        "/srv/igm-models",
        enable_mcp=False,
        restart_gateway=True,
        reboot_host=False,
    )
    validate(config)
    assert set(config["engines"]) == {"llama", "dwarfstar"}
    assert config["model_roots"] == {"models": "/srv/igm-models"}
    assert config["recovery"] == {"restart_gateway": True, "reboot_host": False}
    argv = config["engines"]["llama"]["models"]["example-llama"]["argv"]
    assert "/srv/igm-models/model.gguf" in argv
    assert "mcp" not in config


def test_build_config_rewrites_all_selected_engine_model_paths():
    config = build_config(
        list(ENGINE_IDS),
        "/data/models",
        enable_mcp=False,
        restart_gateway=False,
        reboot_host=False,
    )
    validate(config)
    text = json.dumps(config)
    assert "/opt/models/" not in text


def test_matchers_reduced_to_selected_engines():
    config = build_config(
        ["dwarfstar"],
        "/data/models",
        enable_mcp=False,
        restart_gateway=False,
        reboot_host=False,
    )
    assert config["native_matchers"] == [{"exe": "ds4-server"}]
    full = build_config(
        list(ENGINE_IDS),
        "/data/models",
        enable_mcp=False,
        restart_gateway=False,
        reboot_host=False,
    )
    assert len(full["native_matchers"]) == 9


def test_rejects_empty_and_unknown_engines():
    with pytest.raises(ValueError):
        build_config([], "/data/models", False, False, False)


def test_cli_refuses_overwrite_and_writes_valid_config(tmp_path, capfd):
    output = tmp_path / "config.local.json"
    result = subprocess.run(
        [
            sys.executable,
            str(GENERATOR),
            "--engine",
            "llama",
            "--models-dir",
            str(tmp_path / "models"),
            "--restart-gateway",
            "--output",
            str(output),
        ],
        cwd=ROOT,
        env={**__import__("os").environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    validate(json.loads(output.read_text()))
    result = subprocess.run(
        [
            sys.executable,
            str(GENERATOR),
            "--engine",
            "llama",
            "--models-dir",
            str(tmp_path / "models"),
            "--output",
            str(output),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "refusing to overwrite" in result.stderr


def test_installer_shell_script_passes_bash_syntax_check():
    if not shutil.which("bash"):
        pytest.skip("bash not available")
    result = subprocess.run(
        ["bash", "-n", str(INSTALLER)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
