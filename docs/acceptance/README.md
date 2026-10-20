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

Actual Windows acceptance remains pending. The current MCP controls models; the
coding client's terminal launches OpenCode. A dedicated MCP session-launch tool
is not part of this package.

Package SHA-256: `d65dbe8db5e92db89e04704009f728ce9097d3f139e2b3279026763fd0604d0e`
