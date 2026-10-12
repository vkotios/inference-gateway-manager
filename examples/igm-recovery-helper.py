#!/usr/bin/python3 -I
"""Root-owned fixed-action helper. No client-supplied commands or service names."""

import os
import subprocess
import sys


def main(args):
    if os.geteuid() != 0 or len(args) != 1:
        return 2
    actions = {
        "restart-gateway": ["/usr/bin/systemctl", "restart", "inference-gateway-manager.service"],
        "reboot-host": ["/usr/bin/systemctl", "reboot"],
    }
    if args[0] not in actions:
        return 2
    try:
        # A fixed timer name also prevents duplicate jobs across manager restarts.
        result = subprocess.run(
            [
                "/usr/bin/systemd-run",
                "--quiet",
                "--collect",
                "--unit=igm-recovery",
                "--on-active=3s",
                "--timer-property=AccuracySec=1s",
                *actions[args[0]],
            ],
            env={"PATH": "/usr/bin:/usr/sbin", "LANG": "C"},
            stdin=subprocess.DEVNULL,
            timeout=8,
            check=False,
        )
        return result.returncode
    except (OSError, subprocess.TimeoutExpired):
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
