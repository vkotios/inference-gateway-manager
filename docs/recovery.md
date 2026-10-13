# Unattended boot and remote recovery

## Boot before login (recommended system service)

Install the project, venv, configuration, credentials and weights on storage available before login. Use an **unprivileged engine-owning account** with GPU/container permissions; `igm` below is a placeholder. Do not run the gateway as root.

Edit `examples/inference-gateway-manager-system.service`: replace account/group and `/opt/inference-gateway-manager`. Install the edited file as `/etc/systemd/system/inference-gateway-manager.service`, then:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now inference-gateway-manager.service
sudo systemctl status inference-gateway-manager.service
```

Do not run the user and system units concurrently. Stop/disable the old user manager and competing native engine autostart units as a deliberate deployment step. The system unit pulls in `network-online.target`; configure your distribution's wait-online provider if binding a specific LAN address. It retries failed starts every five seconds with rate limiting disabled. Monitor persistent failures rather than assuming retries fix invalid configuration.

The optional `default` configuration loads that engine/model asynchronously after startup. The control plane starts first; `/health` is not proof of model readiness. Check `/api/status`. Last UI selection and interrupted requests are not restored.

## Recovery permissions (opt-in)

Both controls default to disabled:

```json
"recovery": {"restart_gateway": false, "reboot_host": false}
```

Only the control credential can invoke them. Confirmation is the exact action name. These are explicitly disruptive operations, not ordinary idle-only engine switching: gateway restart may cancel inference and terminate service-owned engines; host reboot interrupts **all machine workloads**.

For the **system service only**, install the fixed-action helper. Inspect it first, then:

```sh
sudo install -d -o root -g root -m 0755 /usr/local/libexec
sudo install -o root -g root -m 0755 examples/igm-recovery-helper.py /usr/local/libexec/igm-recovery
sudo visudo -f /etc/sudoers.d/inference-gateway-manager
```

Use `examples/inference-gateway-manager.sudoers` as the contents, replacing `igm` with the service account. Verify the helper and all parent directories are root-owned and not writable by that account. The helper uses isolated Python (`-I`), accepts only two fixed action tokens, and schedules fixed systemctl operations with a three-second transient system timer. No shell, command text or target unit can be supplied by the API. Do not grant unrestricted sudo/systemctl/systemd-run permissions. Do not use `NoNewPrivileges=yes` for a service using this sudo helper.

Set `restart_gateway` to true to expose gateway restart. Host reboot requires **both** `reboot_host: true` and uncommenting its separate exact sudoers permission. Do not enable it for untrusted control-key holders. Restart the gateway through your deployment procedure to apply config changes.

The helper targets `inference-gateway-manager.service` in the **system** manager, not a user unit. Do not enable these controls on the old user-service deployment. A button can appear enabled from config even if OS permissions are absent; scheduling then returns 503 without claiming success.

## UI, API and CLI

The dashboard has separate **Restart gateway** and **Reboot inference host** buttons with disruption confirmation. It polls during recovery and reconnects with its in-memory credential if the page remains open.

- `GET /api/recovery`: configured capabilities and pending state.
- `POST /api/recovery/restart-gateway`: `{"confirmation":"restart-gateway"}`.
- `POST /api/recovery/reboot-host`: `{"confirmation":"reboot-host"}`.

A 202 response means **scheduled**, not completed. Permission/helper failure is 503; disabled is 403; incorrect confirmation is 400; duplicate pending action is 409. The timer starts after three seconds (systemd scheduling may delay it). Inspect `igm-recovery.timer`, `igm-recovery.service` and the gateway journal if the scheduled action does not execute. A timed-out client must not blindly retry: the OS job may already exist. There is no automatic retry or polling request that reissues recovery.

From another machine, set `IGM_CONTROL_KEY` privately in the environment (never as a command-line argument), then:

```sh
inference-gateway recovery restart-gateway --url https://inference.example.lan --confirm restart-gateway
inference-gateway recovery reboot-host --url https://inference.example.lan --confirm reboot-host
```

CLI TLS verification remains enabled; redirects and environment proxies are not followed. HTTP is suitable only on a trusted isolated LAN or tunnel. After recovery, verify status and model readiness, not merely the accepted response.

## Frozen gateway: external watchdog

The system-service example uses `Type=notify`, `NotifyAccess=main`, `WatchdogSec=30`, and `Restart=always`. The app reports readiness and sends watchdog notifications **from its event loop**. If that loop stalls, systemd times it out, terminates the service/cgroup and restarts it. A dead/stopped process is also restarted. Shutdown escalation is bounded by `TimeoutStopSec`; an explicit systemctl stop is not automatically undone.

This detects process/event-loop failure, not every possible HTTP-route deadlock or stuck native engine. A gateway whose event loop is alive but whose inference backend is stuck still needs force-unload/reload or an explicit recovery action. A completely frozen gateway cannot serve its own button or CLI endpoint; the supervisor is the recovery path. A frozen kernel/driver may require a hardware watchdog or out-of-band management.

Keep `NOTIFY_SOCKET` and watchdog variables private to the manager; engines must not send manager watchdog notifications. A matching PID namespace alone does not verify cross-container visibility or permissions. Detached container engines require real teardown acceptance tests.

## Acceptance and power-loss limits

Verify boot-before-login, configured default load, remote reconnect, watchdog recovery and optional reboot on the **target deployment** during a maintenance window. Unit-file validation and successful scheduling tests do not certify an actual host reboot. Abrupt power loss also requires firmware power-on-after-AC-restoration, headless storage access, and preferably a UPS. No service can resume a lost request or recover a host that has not powered back on.

### Controlled boot acceptance

1. Obtain explicit permission for a host reboot; it interrupts every workload, not just inference. Record `/proc/sys/kernel/random/boot_id`, the configured default, gateway/native process identities and the current deployed version. Check admitted requests and native occupancy before proceeding. Do not ignore reboot inhibitors or force an unknown workload to stop.
2. Arrange a boot-started verifier and a durable evidence location before rebooting. An enabled system service or a lingering user's enabled unit can run the verifier, but a user unit's network target does not order it against system networking. Wait for actual authenticated status and backend readiness rather than assuming target ordering proves readiness.
3. Keep the local console logged out until the verifier finishes. Record that the boot ID changed and no interactive login preceded the readiness and inference checks. PID numbers may repeat across boots; compare the boot identity as well as process identities.
4. Require the configured default engine/model to become `ready`, the expected native context to be loaded, and known idle occupancy. Run a short real inference through `/v1/chat/completions` with the inference credential, then verify the expected output and final ready/idle status. `/health`, `/v1/models` or an enabled unit alone is not an inference pass.
5. Save the receipt and inspect the supervisor journal on failure. A verifier that times out or observes an earlier interactive login must not claim boot-before-login acceptance. Remove or disable temporary verification jobs after inspection. One authorized maintenance reboot does not authorize enabling the permanent `reboot_host` capability or its sudoers rule.

### Second-device LAN acceptance

Use a genuinely separate machine on the allowed LAN, not this host requesting its own interface address. Use TLS or a trusted isolated LAN/tunnel; never put credentials in URLs, command arguments, screenshots or shared logs.

- Open the configured gateway origin in a browser and authenticate with the private **control** key. Confirm the dashboard shows the restored default and live status/telemetry. Check for asset, connection and authorization errors.
- Configure an OpenAI-compatible client with the gateway's `/v1` base URL and private **inference** key. Confirm `/v1/models`, a short Chat Completions request and a streaming request work. Test tool calls separately before relying on a coding workflow.
- Confirm the inference key cannot access `/api/status` (HTTP 403); do not provide the control key to a coding client. Keep native engine ports loopback-only.
- For an explicitly authorized switching test, select an engine before its model and verify the previous native process exited before loading the next. Restore the configured default afterward. Do not use force on somebody else's active work.
- Record the client device, tested endpoints and actual results privately. A successful same-host LAN-address test or short smoke does not establish second-device connectivity, sustained client endurance or power-loss recovery.
