# Register the MCP and companion skill

Registration can use the clients' existing commands. This repository does not yet
provide a single installer that merges all desktop/CLI configurations. Registering
the MCP and installing the skill are separate operations; neither changes API keys.

## Windows: command-based registration

First install the adapter and shared launcher using the [Windows walkthrough](acceptance/windows-mcp/START-HERE.md).
If your existing API keys are already Windows user environment variables, keep
them. The launcher reads those values privately; do not pass key values on the
command line. These commands run on the Windows client, not the gateway host.

For a new `igm` registration, run the relevant command below in PowerShell. Replace
`GATEWAY_HOST` with the **local network IP of the computer running the gateway**,
and change port 9000 if necessary. Paths are derived from your Windows account:

```powershell
$igmPython = Join-Path $env:LOCALAPPDATA 'IGM\venv\Scripts\python.exe'
$igmLauncher = Join-Path $env:LOCALAPPDATA 'IGM\bundle\run-gateway-mcp.py'
$igmUrl = 'http://GATEWAY_HOST:9000'
if ($igmUrl -match 'GATEWAY_HOST') { throw 'Replace GATEWAY_HOST with the gateway computer LAN IP first' }
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

A single Windows setup command could validate the gateway URL, reuse existing
private keys, register the installed clients, set Codex's loading timeout and copy
the companion skill. It should back up configurations, update only `igm`, preserve
other settings, and support repeat runs. Desktop chat JSON merging remains a
separate step today; do not describe CLI registration as covering every surface.

## Client documentation

- [Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
- [Codex skill discovery](https://learn.chatgpt.com/docs/build-skills)
- [Claude Code MCP registration](https://code.claude.com/docs/en/mcp)
- [Claude Desktop configuration sharing](https://code.claude.com/docs/en/desktop)
- [Claude Code skills and cloud scope](https://code.claude.com/docs/en/skills)
