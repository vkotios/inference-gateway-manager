# Interactive installer

`installer/install.sh` is an interactive host setup for a repository checkout of IGM. It asks which engines to configure, where model files live, which recovery actions to expose, whether to install the optional MCP extra, and whether to enable a boot service. It installs no engines, downloads no models, and never runs as root.

## Run it

```sh
cd inference-gateway-manager
bash installer/install.sh
```

Environment overrides for unattended defaults on the prompt answers' target paths:

- `IGM_INSTALL_DIR` — install target (default `/opt/inference-gateway-manager`).
- `IGM_SERVICE_USER` — account name used in the boot-service hints (default: current user).

Every prompt still requires an interactive answer; the overrides only change the suggested defaults.

## What it asks and does

1. **Install directory.** Files are copied there (excluding `.git`, `.venv`, `.local`) unless you install in place, then a venv is created and the package installed into it. An existing installation with credentials or config must be explicitly reconfigured in place; the installer never overwrites `init-keys` output.
2. **Engine selection.** Any of `llama`, `strata`, `kyojin`, `dwarfstar`, `gufo`, a comma-separated subset, or `all`. Unselected engines are removed from the generated config, including their `native_matchers` entries.
3. **Models directory.** Created if missing; every `/opt/models` path in the selected engines' models, profiles and `config_template` values is rewritten to this directory, and it is registered as `model_roots`. IGM never downloads weights: copy your model files there yourself.
4. **Recovery options.** Both `restart_gateway` and `reboot_host` default to disabled. Enabling them only flips the config flags; host reboot additionally needs the sudo helper from [recovery](recovery.md) before the button works.
5. **MCP.** Optionally installs the `mcp` Python extra (`igm-mcp`). The adapter itself runs on client computers; see [MCP registration](mcp-registration.md).
6. **Boot service.** Optionally installs the user systemd unit from `examples/inference-gateway-manager.service` (paths rewritten to your install directory) into `~/.config/systemd/user/`, enables it, and can start it immediately. For power-on start without an interactive login it prints the exact `sudo loginctl enable-linger USER` command; do not run the gateway as root.

The generated `config.local.json` is validated with the same `gateway.config.validate` used at startup, so a broken selection cannot produce an unloadable config. Credentials come from `python -m gateway init-keys`; if the file already exists it is kept untouched.

## Generated config only

The installer writes configuration and services; it does not install engine binaries. `argv` entries such as `/opt/llama/llama-server` or `/opt/ds4/ds4-server` still point at engine installs you maintain separately, per engine. Edit `config.local.json` after installation to set real paths, model names, LAN address, `allowed_hosts` and `allowed_networks` as documented in the [README](../README.md).

## Scripted config generation

`installer/generate_config.py` is the deterministic, testable builder behind the prompts and can be used directly:

```sh
python3 installer/generate_config.py \
  --engine llama --engine dwarfstar \
  --models-dir /srv/models \
  --enable-mcp --restart-gateway \
  --output config.local.json
```

It refuses to overwrite an existing output file with different content and validates the result before writing. Choices for `--engine` are `llama`, `strata`, `kyojin`, `dwarfstar`, `gufo`.
