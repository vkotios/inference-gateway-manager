# Interactive host installer

Run `bash installer/install.sh` from a complete repository checkout on the Linux inference host, as the unprivileged account that will own the gateway and engines. Python 3.11+ with venv/pip support is required. Package installation needs access to the Python package index. Engines, ROCm/CUDA dependencies and compatible weights must be installed independently; this installer does not download weights, install or update engines, or alter GPU settings.

```sh
git clone https://github.com/vkotios/inference-gateway-manager.git
cd inference-gateway-manager
bash installer/install.sh
```

## Setup choices

1. **Installation directory.** Defaults to `~/.local/share/inference-gateway-manager`; `IGM_INSTALL_DIR` changes that suggested default. Use an empty directory separate from the source checkout, or a clean checkout for an in-place installation. Absolute paths are required; the installer expands `~`. Paths with spaces are supported; dollar signs and line breaks in installation paths are rejected. `/opt` needs administrator provisioning if you choose it. Only public source/package directories are copied; credentials, private configs, caches and host artifacts are excluded.
2. **Engines.** Select `llama`, `strata`, `kyojin`, `dwarfstar`, `gufo`, `lucebox`, `ds4a`, a comma-separated subset, or `all`. Empty input offers individual choices. The list comes directly from the public configuration. **ds4 stays available as `dwarfstar`; ds4a is the independent ds4fa fork.** Unselected catalog entries are removed, but all known-engine process matchers remain to detect competing runtimes, including the shared `ds4-server` binary name. Matchers do not grant ownership or permission to kill unmanaged work.
3. **Models directory.** Defaults to `~/models`. Created only after confirmation. Paths rooted at `/opt/models` in selected models, profiles, companion paths and generated engine configuration templates are rewritten to this root. Existing weights are not moved, changed or downloaded.
4. **LAN access.** Defaults to loopback only. LAN setup asks for a specific private interface IP, a client subnet in CIDR notation that contains it, and an optional exact gateway hostname. The IP is always allowed; a supplied hostname is added to `allowed_hosts`. Wildcard binding, public bind IPs, unrestricted client networks and invalid hostnames are refused. The IP must actually belong to this host, and DNS/firewalls must allow the intended clients. Native engine backends remain loopback-only. HTTP is appropriate only on a trusted isolated LAN or tunnel; use a TLS reverse proxy for other deployments as described in the [README](../README.md#lan-security).
5. **Service mode.** Choose manual startup (`none`, the default), a user service, or a prepared system service. No service starts automatically. See below for boot and recovery requirements.
6. **Review and confirm.** The summary names the selected engines, paths, bind IP, host, network and service/recovery choices. Confirmation defaults to no. End-of-input cancels instead of accepting defaults. Invalid inputs are rejected before destination copying or package installation. Installation failures can leave a partial fresh directory; inspect and remove only that failed new installation before retrying.

The generated `config.local.json` is validated by the gateway's startup validator. All sample models remain **disabled**, and no startup default is added. The gateway can start unloaded; choosing an engine or registering weights does not start an inference process. Review the fixed executable paths, profile settings and model artifacts, register your models, then explicitly load one through the dashboard or control API.

The [Lucebox and ds4a guide](lucebox-ds4a.md) explains no-draft Lucebox, ds4fa-native DSpark support GGUFs, model-name mapping, 4K–128K context presets and `gateway_only` occupancy. Presets do not prove hardware capacity or artifact compatibility. See [Kyojin setup](kyojin.md) for its separate EXL3 requirements.

## Credentials and client onboarding

The installer creates independent random control and inference keys through `gateway init-keys`. It prints their private file location, never their values. The credential file is owner-only readable. The control key manages lifecycle; give ordinary inference clients only the inference key. A password for remembered dashboard login is separate from these keys.

The final output provides the selected **root gateway URL** for dashboard/MCP and the `/v1` base URL for inference clients. Open the private credential file on the server to obtain the required key; installation does not share it or discover clients. Follow [client setup](clients.md) and [MCP registration](mcp-registration.md). MCP runs on a **separate client computer**; this host installer does not install the optional MCP extra or create an unused `mcp` config section.

## Boot and recovery

- **Manual (`none`):** prints quoted startup commands for the new config and credentials. Run them only when no supervisor already owns the gateway.
- **User:** creates a new unit in `${XDG_CONFIG_HOME:-~/.config}/systemd/user/`, reloads the user's systemd manager and enables the unit, but does not start it. It refuses to replace an existing unit. For power-on without login it prints `sudo loginctl enable-linger ACCOUNT`; an administrator must run that separately. Recovery API flags remain disabled because their fixed helper targets the system manager. Configure wait-online behavior separately when binding a LAN IP.
- **System:** prepares `.local/inference-gateway-manager.service` with the current account/group and the actual install paths. It prints administrator commands to install it into `/etc/systemd/system/`, reload systemd, enable it and, after review, start it. It never runs sudo or installs/starts a system unit itself. Before applying the commands, inspect any existing unit and deliberately reconcile competing user/system gateway services and native engine autostart. Service files and private storage must be available before login; encrypted/unmounted home directories need a different deployment path.

Only system mode offers gateway-restart and host-reboot capability prompts, both defaulting to disabled. Enabling a config flag does **not** install the root-owned recovery helper or grant OS permission. Complete the fixed-action helper and exact sudoers permissions in [recovery setup](recovery.md#recovery-permissions-opt-in) before using the buttons; do not grant unrestricted sudo. Reboot interrupts all host workloads. No reboot, linger change or recovery action is performed by the installer.

Service enablement or a `/health` response is not proof of boot-before-login, ready models, second-device connectivity or power-loss recovery. Perform the target-host acceptance checks in [recovery](recovery.md#acceptance-and-power-loss-limits) during an authorized maintenance window.

## Existing installations and updates

This is a **fresh-install** workflow. A destination with `config.local.json`, `.local` state/keys or a `.venv` is refused before copying or installing packages. The installer does not offer a misleading reconfigure/overwrite prompt, rotate credentials, remove state or restart a live gateway. A separate nonempty target is also refused.

For a manual gateway upgrade, record the installed revision and environment, back up the private config/credentials/state, and prepare a rollback copy. Review release/config changes and test the new package in a separate environment. During authorized idle maintenance, apply the reviewed code/environment change through the actual supervisor, keeping the existing private config and keys; verify authenticated status, catalog and required inference workflows. Restore the previous environment/config if acceptance fails. This PR does not add an automated gateway or engine updater.

## Config generation without installation

From a checkout with IGM and its dependencies installed (for example `.venv/bin/pip install .`), the same deterministic builder is available separately:

```sh
.venv/bin/python installer/generate_config.py \
  --engine dwarfstar --engine lucebox --engine ds4a \
  --models-dir /srv/models \
  --host 192.168.50.2 --allowed-network 192.168.50.0/24 \
  --allowed-host gateway.lan \
  --output config.local.json
```

Omit the network options for loopback defaults. Replace the sample LAN IP/subnet/hostname with your actual network. `--restart-gateway` and `--reboot-host` are only for a separately prepared system-service deployment with the required helper permissions. `--list-engines` needs only Python's standard library. Existing identical config output is left untouched; different content or symlinks are refused, and new output is created exclusively. This CLI does not install packages, create model directories or activate services.

Tests exercise all seven catalog selections, artifact/context preservation, network validation, no-overwrite behavior, public-file copying and prepared system/user units. Scripted onboarding tests run real config and credential generation with fake package/service commands in temporary directories; they do not certify OS service installation or GPU execution.
