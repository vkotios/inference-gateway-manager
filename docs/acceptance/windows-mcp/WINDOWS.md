# Windows desktop and CLI acceptance

One Windows installation supplies the same six gateway MCP tools to Codex desktop,
Codex CLI, Claude Desktop and Claude Code CLI. The MCP process runs on Windows and
contacts the Linux gateway over the LAN. Use a local STDIO connection, not the LAN
gateway address as an HTTP MCP endpoint. The gateway HTTP API is not an MCP endpoint.

For the complete model-selection → OpenCode workflow, use Codex desktop/CLI or
Claude's local **Code** tab/CLI with terminal access. The current gateway MCP does
not launch OpenCode itself. Claude Desktop chat can use gateway tools, but this bundle
does not add a terminal or an OpenCode-launch tool to chat. Code sessions are the
recommended acceptance surface.

## 1. Install once

Keep the original remote-check.zip files and extract this Windows add-on into the same
permanent directory, for example
`C:\Users\YOUR_NAME\AppData\Local\IGM\bundle`. Open PowerShell in that combined
directory. The original wheel is unchanged; no second transfer is needed. Python 3.11
or newer must already be installed. If its environment is already installed, reuse it
and skip creation/installation below. Otherwise run:

```powershell
$igmRoot = Join-Path $env:LOCALAPPDATA 'IGM'
py -3 -c "import sys; assert sys.version_info >= (3, 11), 'Python 3.11+ required'"
if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.11 or newer first' }
py -3 -m venv (Join-Path $igmRoot 'venv')
if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python environment' }
$igmPython = Join-Path $igmRoot 'venv\Scripts\python.exe'
$igmWheel = (Resolve-Path '.\inference_gateway_manager-0.1.0-py3-none-any.whl').Path
& $igmPython -m pip install "${igmWheel}[mcp]"
if ($LASTEXITCODE -ne 0) { throw 'Package installation failed' }
$igmLauncher = (Resolve-Path '.\run-gateway-mcp.py').Path
Write-Host "Python: $igmPython"
Write-Host "Launcher: $igmLauncher"
```

Keep the extracted bundle at that location after configuring the clients. No model
files or engine binaries are installed on Windows. Install OpenCode on Windows
separately if it is not already available.

## 2. Supply the test credentials privately

Use the control and inference credentials configured for your gateway.
Set up credentials privately with your gateway administrator.

Obtain those values through your trusted local/SSH workflow. Never put them in chat,
a task prompt, command-line arguments, a shared repository, or this bundle. The
following prompts hide entry and store values in the current Windows account's user
environment. User environment storage is not an encrypted credential vault; other
programs running as that Windows account can read it. This is an acceptance setup.

```powershell
foreach ($igmName in @('IGM_CONTROL_KEY', 'IGM_INFERENCE_KEY')) {
    $igmSecret = Read-Host "Enter $igmName privately" -AsSecureString
    $igmValue = [System.Net.NetworkCredential]::new('', $igmSecret).Password
    if ($igmValue.Length -lt 32 -or $igmValue -match '\s') {
        throw 'The key must be at least 32 characters with no whitespace'
    }
    [Environment]::SetEnvironmentVariable($igmName, $igmValue, 'User')
    Remove-Variable igmValue, igmSecret
}
```

The shared launcher reads those two user-environment entries itself, so desktop
clients do not need duplicate plaintext keys in each configuration. It prints no
credentials and emits MCP protocol messages only on standard output.

## 3. Codex desktop and native Windows CLI

Merge codex-windows.toml into `%USERPROFILE%\.codex\config.toml`, replacing
YOUR_NAME with your Windows account folder and GATEWAY_HOST with your gateway host. Preserve other settings and servers.
Use the actual absolute Python and launcher paths printed in step 1 if different.

The desktop app and native Windows Codex CLI share this configuration for the same
Codex host. Restart/reconnect the MCP server and begin a new task. The template gives
model-loading tool calls 900 seconds. Run `codex mcp list` if using the CLI.

If your CLI or app agent runs in WSL, its Linux paths, Python environment and Codex
home are separate. This bundle's Windows paths target Windows-native clients;
configure WSL separately rather than copying these paths into a Linux configuration.

## 4. Claude Desktop chat and Code tab

Merge the `mcpServers.igm` entry from claude-desktop-windows.json into
`%APPDATA%\Claude\claude_desktop_config.json`. Replace YOUR_NAME and GATEWAY_HOST, and use the actual
paths from step 1. Do not replace the whole configuration if it already has servers.
Quit Claude Desktop completely and reopen it. Inspect the local MCP connection and
confirm the six gateway tools are listed.

Current Claude Desktop versions can also use this entry in local Code-tab sessions.
For an actual OpenCode coding task, select a local Code session with terminal access.
Client versions and managed policies can affect server discovery; verify tools in
each surface rather than assuming a chat connection proves Code/CLI connectivity.

## 5. Claude Code CLI

The standalone CLI does not read claude_desktop_config.json. Register the same
launcher separately in user scope from the PowerShell session in step 1:

```powershell
claude mcp add --scope user --transport stdio igm -- "$igmPython" "$igmLauncher" --url http://GATEWAY_HOST:9000
```

If a new PowerShell session has lost those two path variables, use their actual
absolute paths in the command instead. Inspect an existing igm entry before changing
it. In a new Claude Code session, `/mcp` should show igm and its six tools. CLI user
configuration is also read by the Code tab. Prefer one definition per surface; if
both Desktop and CLI define igm, current local Code sessions prefer the Desktop entry.

## 6. Run the intended acceptance task

Copy ACCEPTANCE-PROMPT.txt into a local Codex desktop task, Claude Code-tab session,
or the corresponding CLI. It uses MCP for model selection/loading and the parent's
terminal tools to launch real OpenCode sessions on Windows. Work sequentially because
the gateway serves one backend and one inference request at a time.

OpenCode needs its own inference credential. When launching it, set only
IGM_INFERENCE_KEY in its environment (reading the Windows user value if needed), and
remove IGM_CONTROL_KEY from that child's environment. Keep the control credential
inside the MCP/controller. The included OpenCode configuration fragment references
the inference environment variable; it contains no key value.

If a load times out, inspect gateway_status before retrying: client timeout does not
prove the backend failed or was cancelled. The portable check-remote-mcp.py remains
an optional transport probe; it is not a substitute for real OpenCode coding sessions.

Report versions, selected models, context/reasoning, session IDs, test results and
sanitized failures. Do not include keys or private source content. 

## Sources checked

- OpenAI MCP configuration: https://learn.chatgpt.com/docs/extend/mcp?surface=cli
- OpenAI Windows/WSL configuration: https://learn.chatgpt.com/docs/windows/windows-app
- Claude Desktop/CLI configuration: https://code.claude.com/docs/en/desktop
- Claude Code MCP setup: https://code.claude.com/docs/en/mcp
- Python MCP SDK host setup: https://py.sdk.modelcontextprotocol.io/get-started/real-host/
