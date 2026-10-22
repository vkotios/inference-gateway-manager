# Complete Windows MCP setup

Use **one package**: [windows-mcp-setup.zip](windows-mcp-setup.zip).
It contains the Python wheel, shared launcher, client configuration templates,
acceptance prompt and complete setup guide. Earlier ZIPs are superseded;
there is no archive merging step.

[Read the complete instructions online](windows-mcp/START-HERE.md), including the
PowerShell block that downloads and extracts the single package to the right folder.

The public package contains no credentials, actual gateway address, private
deployment paths, model files, state, logs or backups. Replace GATEWAY_HOST with the
local network IP of the computer running the gateway. Enter credentials privately
as described in the guide.

Windows acceptance completed on 2026-10-20: the Windows Codex CLI reached the LAN
gateway through MCP and OpenCode created files using gateway inference. Independent
test execution passed. The OpenCode session did not invoke its terminal tool, so
OpenCode-run test execution remains a separate harness-capability check. The current
MCP controls models; the coding client's terminal launches OpenCode. A dedicated MCP
session-launch tool is not part of this package.

Package SHA-256: `27695bd24334333692395091466bb11b9443e89171366812ff0669dd3f8f4467`
