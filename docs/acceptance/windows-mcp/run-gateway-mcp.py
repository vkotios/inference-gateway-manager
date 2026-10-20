"""Shared acceptance launcher for Windows desktop and CLI MCP clients."""

import os
import runpy
import sys

for name in ("IGM_CONTROL_KEY", "IGM_INFERENCE_KEY"):
    value = None
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                value, kind = winreg.QueryValueEx(key, name)
                if kind not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
                    value = None
        except FileNotFoundError:
            pass
    value = value or os.environ.get(name)
    if not isinstance(value, str) or len(value) < 32 or any(c.isspace() for c in value):
        sys.stderr.write("Set " + name + " privately in the Windows user environment.\n")
        raise SystemExit(1)
    os.environ[name] = value

sys.argv = ["gateway.mcp", *sys.argv[1:]]
runpy.run_module("gateway.mcp", run_name="__main__")
