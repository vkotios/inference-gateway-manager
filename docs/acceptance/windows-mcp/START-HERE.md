# Complete Windows MCP setup

**Use only `windows-mcp-setup.zip`. It contains the wheel and all setup files.
The earlier ZIPs are superseded: do not extract or combine them for this setup.**

You can read this guide from your phone. Run the steps when you have access to your
Windows PC; you do not need access to that PC just to read the instructions.

This installs one local MCP adapter for Codex desktop/CLI and Claude Desktop/Code
CLI. For the OpenCode task, use Codex or Claude's local **Code** tab with terminal
access. MCP selects and loads models; the coding client's terminal starts OpenCode.
The gateway MCP itself has no session-launch tool.

## Before starting

You need Python 3.11+ with the Windows `py` launcher, your gateway base URL, and its
two API keys: control and inference. The Windows PC must be able to reach the gateway.
**Your dashboard password is separate from these API keys.** Obtain the keys privately;
they are not in any download. OpenCode must be installed for the final coding test.

## 1. Download and extract ONE package

Open PowerShell on Windows and run this entire block. It downloads the complete ZIP
and extracts its contents directly into the permanent folder:

```powershell
$igmRoot = Join-Path $env:LOCALAPPDATA 'IGM'
$igmBundle = Join-Path $igmRoot 'bundle'
$igmZip = Join-Path $env:TEMP 'windows-mcp-setup.zip'
New-Item -ItemType Directory -Path $igmBundle -Force | Out-Null
Invoke-WebRequest -UseBasicParsing -Uri 'https://raw.githubusercontent.com/vkotios/inference-gateway-manager/codex/gateway-controls/docs/acceptance/windows-mcp-setup.zip' -OutFile $igmZip
Expand-Archive -LiteralPath $igmZip -DestinationPath $igmBundle -Force
Invoke-Item $igmBundle
```

The folder that opens should contain these six files directly, with no extra
ZIP-name folder between `bundle` and the files:

```text
%LOCALAPPDATA%\IGM\bundle\
  START-HERE.md
  inference_gateway_manager-0.1.0-py3-none-any.whl
  run-gateway-mcp.py
  codex-windows.toml
  claude-desktop-windows.json
  ACCEPTANCE-PROMPT.txt
```

Keep this folder in place. Clients will launch the adapter from it. Model weights
remain on the gateway host.

## 2. Install the adapter

Run this entire block. Each block in this guide sets its own path variables, so a
new PowerShell window is fine. An existing dedicated Python environment is reused.

```powershell
$igmRoot = Join-Path $env:LOCALAPPDATA 'IGM'
$igmPython = Join-Path $igmRoot 'venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $igmPython)) {
    py -3 -c "import sys; assert sys.version_info >= (3, 11), 'Python 3.11+ required'"
    if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.11+, then repeat step 2' }
    py -3 -m venv (Join-Path $igmRoot 'venv')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python environment' }
}
& $igmPython -c "import sys; assert sys.version_info >= (3, 11), 'Python 3.11+ required'"
if ($LASTEXITCODE -ne 0) { throw 'The existing IGM environment needs Python 3.11+' }
$igmWheel = Join-Path $igmRoot 'bundle\inference_gateway_manager-0.1.0-py3-none-any.whl'
& $igmPython -m pip install "${igmWheel}[mcp]"
if ($LASTEXITCODE -ne 0) { throw 'Resolve the package installation error before continuing' }
& $igmPython -c "import gateway.mcp; print('MCP adapter installed')"
```

Continue after it prints `MCP adapter installed`.

## 3. Enter the API keys privately

Run this block and paste each API key at its hidden prompt. The launcher reads these
Windows user-environment entries for both desktop and CLI clients. This storage is
not an encrypted vault; programs running as your Windows account can read it.

```powershell
foreach ($igmName in @('IGM_CONTROL_KEY', 'IGM_INFERENCE_KEY')) {
    $igmSecret = Read-Host "Enter $igmName privately" -AsSecureString
    $igmValue = [System.Net.NetworkCredential]::new('', $igmSecret).Password
    if ($igmValue.Length -lt 32 -or $igmValue -match '\s') {
        throw 'Expected an API key of at least 32 characters, without whitespace'
    }
    [Environment]::SetEnvironmentVariable($igmName, $igmValue, 'User')
    Remove-Variable igmValue, igmSecret
}
```

Use the keys for the gateway instance being tested. Do not substitute your browser
password or put keys into the downloaded templates or chat.

## 4. Complete the three text files

In the permanent `bundle` folder, edit and save:

| File | Replacements |
|---|---|
| `codex-windows.toml` | YOUR_NAME → your Windows user-folder name; GATEWAY_HOST → your gateway host |
| `claude-desktop-windows.json` | The same two replacements |
| `ACCEPTANCE-PROMPT.txt` | GATEWAY_HOST → your gateway host |

The examples use port 9000. If your gateway uses another port, change that too.
To see the exact executable and launcher paths for your account, run:

```powershell
Join-Path $env:LOCALAPPDATA 'IGM\venv\Scripts\python.exe'
Join-Path $env:LOCALAPPDATA 'IGM\bundle\run-gateway-mcp.py'
```

The templates' `command` must name that Python executable, and their first argument
must name that launcher. Preserve the JSON double backslashes in the Claude template.
Neither configuration needs an API-key value.

## 5. Connect the clients you use

### Codex desktop and native Windows CLI

Open `%USERPROFILE%\.codex\config.toml`. Paste the completed contents of
`codex-windows.toml` into it. If `[mcp_servers.igm]` already exists, replace that
section instead of adding a duplicate. Preserve other settings and servers.
Save, restart/reconnect Codex's MCP server, and begin a new local task.

Desktop and native Windows CLI share this configuration for the same Codex host.
The template allows 900 seconds for model-loading tool calls. WSL has separate Linux
paths/configuration and is outside this native-Windows setup.

### Claude Desktop

Open `%APPDATA%\Claude\claude_desktop_config.json`. If it does not exist, create it
using the completed `claude-desktop-windows.json` template.

If it already has servers, add or replace only `igm` inside its existing `mcpServers`
object. Keep the other entries and valid JSON commas. Quit Claude Desktop completely,
reopen it, and verify that the local `igm` server exposes six gateway tools.
For the OpenCode task, use a local **Code** session with terminal access.

### Claude Code CLI

Register the same launcher separately. Replace GATEWAY_HOST before running this
whole block; change port 9000 too if your gateway uses another port:

```powershell
$igmRoot = Join-Path $env:LOCALAPPDATA 'IGM'
$igmPython = Join-Path $igmRoot 'venv\Scripts\python.exe'
$igmLauncher = Join-Path $igmRoot 'bundle\run-gateway-mcp.py'
claude mcp add --scope user --transport stdio igm -- "$igmPython" "$igmLauncher" --url http://GATEWAY_HOST:9000
```

If `igm` is already registered, inspect and update that entry instead of adding a
duplicate. In a new CLI session, `/mcp` should show the connection. Current Claude
Desktop local Code sessions also read CLI configuration; when both Desktop and CLI
define `igm`, local Code sessions prefer the Desktop definition.

## 6. Run the OpenCode acceptance task

Check `opencode --version` in a Windows terminal. Then paste the completed
`ACCEPTANCE-PROMPT.txt` into Codex desktop, Claude's local Code tab, or either coding
CLI. It asks the parent to:

1. Discover and load a supported gateway model through MCP.
2. Start a real OpenCode session with that model and a smaller client budget.
3. Have OpenCode edit files and run a test in a disposable workspace.
4. Verify through MCP that backend model/settings stay unchanged during the session.
5. Finish the session before switching models and repeating sequentially.

Give OpenCode only the inference key; remove the control key from that child's
environment. A `gateway_delegate` text answer does not count as an OpenCode session.
If a load times out, inspect `gateway_status` before retrying: it may still be loading.
Do not force interruption to get past a busy response.

Report client versions, selected models/settings, OpenCode session IDs, test results
and sanitized failures. Actual Windows acceptance remains pending until these checks
run on your PC.

## Reference documentation

- [Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
- [Codex on Windows and WSL](https://learn.chatgpt.com/docs/windows/windows-app)
- [Claude desktop and CLI configuration](https://code.claude.com/docs/en/desktop)
- [Claude Code MCP registration](https://code.claude.com/docs/en/mcp)
