# Register the MCP and companion skill

Registration can use the clients' existing commands. This repository does not yet
provide a single installer that merges all desktop/CLI configurations. Registering
the MCP and installing the skill are separate operations; neither changes API keys.

## Obtain the server connection details

The server owner supplies three values to a trusted MCP controller: the gateway
root URL, control key and inference key. Registration cannot infer these from a
repository download or the client's own IP address. The dashboard currently has
no key-reveal/pairing wizard, and the client does not automatically discover servers.

### On the Linux AI server

1. During first setup, the README runs:

   ```sh
   .venv/bin/python -m gateway init-keys --output .local/credentials.env
   ```

   This generates two independent random keys, writes `IGM_CONTROL_KEY` and
   `IGM_INFERENCE_KEY` to an owner-only file (mode `0600`), and prints only the file
   location. There are no built-in default keys. The server refuses identical keys.
   The command refuses to overwrite an existing file. Do not rerun it to register
   another client; reuse the existing deployment's keys.

2. Open that file privately in a local editor on the server to copy the needed
   values. For example, from the checkout used for the README installation:

   ```sh
   nano .local/credentials.env
   ```

   A service installation may use another credential file: check its configured
   environment-file location. The running gateway uses keys supplied in its
   environment; confirm you are reading the file that deployment actually loads.
   Keep the file private. Share both values only with a trusted controller; an
   inference-only worker needs just the inference key. Do not paste keys into chat.

3. Find the server's network-interface addresses:

   ```sh
   ip -brief -4 address show scope global
   ```

   Choose the Ethernet/Wi-Fi address on the same LAN as the client. Multiple
   addresses can include VPN/container networks; do not blindly pick the first.
   Configure IGM's `host`, `allowed_hosts` and `allowed_networks` for that interface
   as described in [LAN setup](../README.md#lan-security). Defaults bind loopback
   and cannot serve another computer. `127.0.0.1` on the client means the client
   itself; `0.0.0.0` is a bind address, not the address to enter on the client.

4. Give the client the full root URL, `http://GATEWAY_HOST:9000`, replacing
   `GATEWAY_HOST` with the chosen **server** LAN IP and using the configured port.
   Do not add `/v1` for MCP; inference clients use that suffix separately. A stable
   LAN hostname can also be used if it resolves and is allowed by the gateway.
   Prefer a router DHCP reservation or stable DNS so the address does not change.

The keys remain stable across restarts/updates when the same environment is used.
They differ between independently initialized deployments. Replacing the server's
keys requires updating its clients; changing the server IP requires updating their
URLs unless a stable hostname handles it. Neither change happens on registration.

### On the Windows client

Use the existing [hidden API-key prompts](acceptance/windows-mcp/START-HERE.md#3-enter-the-api-keys-privately)
once to paste the server's two values into Windows user environment variables.
Skip that step if those variables already contain the correct keys. The shared
launcher reads them when either desktop or CLI starts the MCP; key values are not
stored in the client registration JSON/TOML or passed on command lines. Windows
user environment variables are not an encrypted vault.

The registration block below asks for the gateway URL in a normal terminal field
(it is not secret). After registration, verify authenticated `gateway_catalog` and
`gateway_status`. There is no GUI popup or automatic server/key discovery today.

## Windows: command-based registration

First install the adapter and shared launcher using the [Windows walkthrough](acceptance/windows-mcp/START-HERE.md).
If your existing API keys are already Windows user environment variables, keep
them. The launcher reads those values privately; do not pass key values on the
command line. These commands run on the Windows client, not the gateway host.

For a new `igm` registration, run the relevant command below in PowerShell. At the
URL prompt, enter the full address obtained from the server owner above. Paths are
derived from your Windows account:

```powershell
$igmPython = Join-Path $env:LOCALAPPDATA 'IGM\venv\Scripts\python.exe'
$igmLauncher = Join-Path $env:LOCALAPPDATA 'IGM\bundle\run-gateway-mcp.py'
$igmUrl = (Read-Host 'Gateway URL using the AI server LAN IP and configured port').Trim().TrimEnd('/')
if ($igmUrl -notmatch '^https?://[^/\s]+$' -or $igmUrl -match 'GATEWAY_HOST') {
    throw 'Enter the full gateway root URL with the actual server LAN IP; no /v1 suffix'
}
if (-not (Test-Path -LiteralPath $igmPython)) { throw 'Install the IGM adapter first' }
if (-not (Test-Path -LiteralPath $igmLauncher)) { throw 'Install the shared launcher first' }

# Codex desktop and native Windows CLI (requires the codex CLI):
codex mcp add igm -- "$igmPython" "$igmLauncher" --url "$igmUrl"
if ($LASTEXITCODE -ne 0) { throw 'Codex registration failed; inspect its existing igm entry' }
```

For Claude Code, use the same variable setup above, then:

```powershell
claude mcp add --scope user --transport stdio igm -- "$igmPython" "$igmLauncher" --url "$igmUrl"
if ($LASTEXITCODE -ne 0) { throw 'Claude Code registration failed; inspect its existing igm entry' }
```

Run only the command for clients you have installed. These are registration
commands, not an idempotent update script. If `igm` already exists, inspect it and
update that entry using the [existing templates](acceptance/windows-mcp/START-HERE.md#5-connect-the-clients-you-use);
preserve other servers and settings. No administrator access is required.

In Codex's `%USERPROFILE%\.codex\config.toml`, ensure the existing
`[mcp_servers.igm]` table also has the following fields, without duplicating the
table. CLI registration alone does not set this model-loading timeout:

```toml
startup_timeout_sec = 30
tool_timeout_sec = 900
```

Codex desktop and CLI share the same host configuration. Native Windows and WSL
have separate paths and configuration. Current Claude Desktop **local Code**
sessions can read Claude Code configuration; Desktop chat additionally uses
`%APPDATA%\Claude\claude_desktop_config.json`. Configure that file using the
walkthrough when you use chat. If both define `igm`, Desktop local Code prefers
the Desktop definition, so keep their URLs consistent.

Restart/reconnect the clients and start a new local coding session. Verify that
`gateway_catalog` and `gateway_status` succeed before loading a model. A registered
entry alone does not prove the gateway is reachable. A timeout requires checking
the URL, gateway service and LAN reachability, not changing working credentials.

## Install the companion skill

The Python wheel installs tools, not skills. The acceptance ZIP also does not
contain the skill. Obtain a trusted checkout of this repository and copy the
**whole** `skills/inference-gateway-manager` folder, including `references`, into
your client's skill directory:

| Local coding client | Personal skill folder on Windows |
|---|---|
| Codex desktop/native CLI | `%USERPROFILE%\.agents\skills\inference-gateway-manager` |
| Claude Code CLI/Desktop local Code | `%USERPROFILE%\.claude\skills\inference-gateway-manager` |

For example, from the repository root in PowerShell, run the copy for each client
you use. Inspect any existing same-named skill before replacing it:

```powershell
$igmSkill = Resolve-Path '.\skills\inference-gateway-manager'
$igmDestinations = @(
    (Join-Path $env:USERPROFILE '.agents\skills\inference-gateway-manager'),
    (Join-Path $env:USERPROFILE '.claude\skills\inference-gateway-manager')
)
foreach ($igmDestination in $igmDestinations) {
    if (Test-Path -LiteralPath $igmDestination) { throw "Skill already exists: $igmDestination; review it before updating" }
}
foreach ($igmDestination in $igmDestinations) {
    New-Item -ItemType Directory -Path (Split-Path $igmDestination) -Force | Out-Null
    Copy-Item -LiteralPath $igmSkill.Path -Destination $igmDestination -Recurse
}
```

Start a new session and explicitly request the `inference-gateway-manager` skill
for the first test. Claude Desktop chat/Cowork and genuinely remote cloud execution
have different skill distribution mechanisms; local personal folders are not a
universal cloud installation. The controlling session needs access to the MCP
**and** a local terminal/workspace to launch OpenCode. Using a cloud model in a
local coding app is sufficient; a remote cloud runner cannot reach a private LAN
address merely because you installed this locally.

## What a future unified installer should do

A single Windows setup command should ask for the full gateway URL, reuse existing
private keys or request missing ones through hidden terminal prompts, and validate
connectivity and both credential roles before claiming setup succeeded. It should
register the installed clients, set Codex's loading timeout and copy the companion
skill. Back up configurations, update only `igm`, preserve other settings, and
support repeat runs. If server-side address detection is added, show interface
candidates and ask the owner to choose when ambiguous; never assume the first IP.
Desktop chat JSON merging remains a separate step today; do not describe CLI
registration as covering every surface. This unified installer is not implemented.

## Client documentation

- [Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
- [Codex skill discovery](https://learn.chatgpt.com/docs/build-skills)
- [Claude Code MCP registration](https://code.claude.com/docs/en/mcp)
- [Claude Desktop configuration sharing](https://code.claude.com/docs/en/desktop)
- [Claude Code skills and cloud scope](https://code.claude.com/docs/en/skills)
