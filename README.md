<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/logo-dark.svg">
  <img src="docs/assets/logo-light.svg" alt="IGM Circuit Relay" width="300">
</picture>

# Inference Gateway Manager

**One engine. One model. One gateway.**

[Host setup](#install) · [Client setup](docs/clients.md#generic-openai-compatible-client) · [Agent/controller guide](docs/orchestration.md) · [Agent skill](skills/inference-gateway-manager/SKILL.md) · [Boot/recovery](docs/recovery.md)

A small Linux control plane for **llama.cpp, Strata, Kyojin and DwarfStar**. Plain HTML/CSS/JavaScript UI; FastAPI API and streaming inference proxy. No orchestration plugin, cloud inference, model download, shell-command API, or frontend build chain.

## What it does

- Explicit engine selection **before** model selection.
- Stops the previous engine, including tracked container children, and verifies native process exit before launching the next.
- PID/start-time identity plus Linux pidfds prevent signals being redirected by PID reuse.
- Refuses ordinary switches while work is active or native slot occupancy is unknown. Force is explicit and cancels inference or an in-progress model load.
- Single admitted inference request; competitors get HTTP 409 rather than an invisible queue.
- Tracks requests, processing state, CPU, RAM pressure and Linux DRM GPU utilization/memory/temperature.
- Separate control and inference credentials. No credentials, argv or filesystem paths in the public catalog.
- Unknown counters remain null. RAM pressure is total minus available, **not model RSS**. Unified GPU memory counters may overlap RAM.

## Host versus client

- **Inference host:** Linux; runs IGM, the engines and local weights. Configure static launch commands, LAN restrictions and optional boot/recovery services here.
- **Client machine:** Windows, macOS or Linux; needs only an OpenAI-compatible client or HTTP-capable agent. All configured models share `http://GATEWAY_HOST:9000/v1` and the inference credential.
- **Trusted controller:** calls `http://GATEWAY_HOST:9000/api/...` with the control credential to discover, select, load and unload models. Ordinary workers should receive only the inference key.

`/` is the dashboard. `/v1` is an **API base URL, not a web page**; a browser GET can return 404. Test `/v1/models` with a Bearer header instead. Registering a model in a client does not load it; load through the dashboard or controller first. Exactly one inference request is admitted at a time, with no invisible queue.

For a walkthrough, Windows credentials, a smoke test and status-code troubleshooting, see [client setup](docs/clients.md). For multiple agents, see [orchestration](docs/orchestration.md). Environment variables are a credential convention, not an automatic integration.

## Install

Clone this repository and enter it before running the commands below, or install a published wheel with `pip install PATH_TO_WHEEL` and obtain the example configuration separately. The editable installation below includes development tools; use `pip install .` for a source-based runtime installation without those tools.

Requires Linux with pidfd support, Python 3.11+, and independently installed engines/weights.

```sh
git clone https://github.com/vkotios/inference-gateway-manager.git
cd inference-gateway-manager
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp examples/config.example.json config.local.json
.venv/bin/python -m gateway init-keys --output .local/credentials.env
# Edit config.local.json: paths, argv, model names, LAN IP and allowed networks.
set -a
. .local/credentials.env
set +a
.venv/bin/python -m gateway serve --config config.local.json --state-dir .local/state
```

Open the configured address and use the control key from your private credential file. Keys are held in browser memory only, not localStorage. `init-keys` refuses to overwrite credentials. All `*.local.json`, `.local/`, `.env*`, logs and weights stay out of Git. Run one manager process/worker. Do not keep independent engine autostart services enabled alongside it.

## Kyojin

The [example configuration](examples/config.example.json) exposes four engine options. The API catalog and dashboard populate from configuration; no hardcoded UI adapter is required. For Kyojin launch details, ROCm runtime requirements, model IDs and acceptance steps, see [Kyojin integration](docs/kyojin.md). Updating the source example does not update an installed gateway's private configuration or reload its process.

## LAN security

Defaults bind loopback. For LAN use bind **the specific private interface address**, add that host to `allowed_hosts`, and its subnet to `allowed_networks`. Keep every native engine loopback-only and use the gateway for inference. No reverse-proxy trust, wildcard CORS or arbitrary command execution is enabled.

HTTP is plaintext even on a LAN: use a trusted isolated LAN, SSH tunnel, or a TLS reverse proxy when confidentiality matters. Do not forward the port to the internet. A control key permits starting the administrator-configured programs; only trusted operators should receive it. Config files are trusted executable configuration, never client supplied. Firewall configuration is deployment-specific. For a TLS reverse proxy, preserve Host, add the browser-facing hostname to `allowed_hosts`, and set an exact origin such as `"allowed_origins": ["https://inference.example.lan"]`. Forwarded headers are not trusted; no wildcard origins are accepted. `allowed_origins` must be a list of exact HTTP/HTTPS origins with valid hosts and optional ports; credentials, paths (including a trailing slash), queries, fragments, whitespace and wildcard hosts are rejected at configuration load. Entries are not silently normalized; use the exact browser origin.

Startup refuses an unreadable/malformed `/proc/self/stat` or a PID different from `os.getpid()`, before manager initialization. Mount `/proc` from the manager's own PID namespace. This sanity check does not prove that detached container engines are visible or inspectable; permissions, ownership-tag forwarding and real container teardown still require acceptance testing.

## Boot and recovery

For the product's recommended boot-before-login system service, optional recovery buttons/API/CLI, scoped OS permissions and event-loop watchdog, see [unattended boot and recovery](docs/recovery.md). Recovery is disabled by default; the existing user-service example remains available.

The example is a **user systemd service**. Enabling it starts it with the user's service manager; enable lingering (`sudo loginctl enable-linger USER`) to start that manager at boot without an interactive login and keep it running after logout. Install the edited example in `~/.config/systemd/user/`, then use `systemctl --user daemon-reload` and `systemctl --user enable --now inference-gateway-manager.service`. Run as the engine-owning user, not root. Alternatively, a system service needs an explicit unprivileged `User=` and `WantedBy=multi-user.target`.

`Restart=on-failure` recovers from manager failures, subject to systemd restart-rate limits. Boot starts a fresh manager: configured `default.engine` and `default.model` are loaded asynchronously, not the last UI selection or interrupted inference requests. Without a default, the control plane starts unloaded. API availability does not prove that the default model is ready; check `/api/status`. Native engines must not have competing autostart services.

Boot recovery requires the machine to power on again, disks and model paths to be available without login, and any containers/GPU services to support headless operation. User-unit `After=network-online.target` alone does not guarantee host networking is ready; account for this when binding a specific LAN IP. Automatic power-on after AC restoration is a firmware setting, and a UPS reduces abrupt power loss. Service enablement is configuration evidence, not a tested power-loss recovery guarantee.

## API

Authenticate `/api/*` with the control credential using the `Authorization` header and `Bearer` scheme. The inference key can only access `/v1/*`. Control keys can also infer.

| Method | Endpoint | Body / behavior |
|---|---|---|
| GET | `/api/status` | Selection, phase, occupancy source, active requests, PID identities |
| GET | `/api/engines` | Sanitized engine/model catalog |
| GET | `/api/telemetry` | Real host counters |
| POST | `/api/engine` | `{"engine":"strata","force":false}`; stops old engine; does not load a model |
| POST | `/api/model` | `{"model":"qwen3.8-flash-next","force":false}`; loads selected engine's model |
| POST | `/api/unload` | `{"force":false}`; stops engine, retains engine selection |
| GET | `/v1/models` | Enabled models and loaded/unloaded status |
| POST | `/v1/chat/completions` | Transparent Chat Completions proxy, including raw SSE |
| POST | `/v1/completions`, `/v1/responses`, `/v1/messages` | Passed through only if the selected backend implements them |

Add `X-Client-Name` to label requests. Model IDs must match the currently loaded gateway model. Selecting a model in a coding client's picker **does not automatically switch the engine**. A future orchestration plugin should call control APIs first. HTTP 409 means busy/invalid lifecycle (including unknown native occupancy on a non-force stop); inference uses 503 for unknown occupancy, and startup or shutdown failures also use 503; 401/403 means credential/role rejection. Startup may take several minutes; `/api/status` remains available while loading. A force-cancelled loader receives 409 and unload retains the selected engine without a startup-error message. Inference JSON must contain finite numbers and at most 128 nesting levels (invalid payloads return 400).

llama.cpp, Strata and Kyojin use native `/slots` in addition to tracked requests. DwarfStar currently uses `probe: gateway_only`: processing is measured at this proxy, **not native slot telemetry**. Direct loopback callers bypass that measurement and must not be used. Never label this mode as verified native idle. An unmanaged native engine blocks loading; reconcile its supervisor/process externally rather than silently killing unrelated work. Native matchers detect conflicts, not ownership; use distinctive argv tokens for Python/module launches (for example `serve.server` for Strata). The manager owns its launched process, descendants and session members. Detached container-exec children must inherit `IGM_ENGINE_RUN_ID`; forward it explicitly with an administrator-configured argv argument such as `"IGM_ENGINE_RUN_ID={launch_id}"` to the container environment. A native match alone never authorizes signalling. Unreadable or missing ownership tags fail closed; check container permissions before relying on teardown.

## Clients

See [client configuration](docs/clients.md). The stable base URL is `http://GATEWAY_IP:9000/v1`; client credentials use the **inference** key, not the control key.

## Agent skill and branding

[`skills/inference-gateway-manager/SKILL.md`](skills/inference-gateway-manager/SKILL.md) is a portable operating procedure for HTTP-capable AI agents. Install it using your agent harness's documented skill mechanism; merely cloning the repository does not load it. It does not implement orchestration, grant permissions or enforce a distributed lock.

The Coral/Bone Circuit Relay mark retains a faded third input to suggest expandability, not measured runtime status. Dark/light/monochrome SVGs, a favicon and a social-preview image are included; see [brand assets](docs/branding.md). GitHub's custom social preview requires a separate repository-settings upload.

## Development

```sh
.venv/bin/python -m pytest -q
.venv/bin/ruff check gateway tests
.venv/bin/ruff format --check gateway tests
.venv/bin/python -m build
node --check gateway/static/app.js
```

Tests use disposable fixture servers, not simulated model benchmarks. Real engine acceptance is separate. Test an engine's tool calls and streaming before trusting a full coding workflow. No general tool-call compatibility claim is made.

## Prior art

[llama-swap](https://github.com/mostlygeek/llama-swap) and [llamactl](https://github.com/lordmathis/llamactl) were investigated for swapping/proxying engines. Their process-based proxy patterns informed the design, but no upstream code was copied. This implementation adds explicit engine-then-model control, PID-identity teardown and a small host dashboard. Engine licenses and model licenses remain independent of this project's MIT license.
