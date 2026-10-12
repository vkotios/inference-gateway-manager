import subprocess
import sys


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
