# Windows acceptance add-on

[Download the Windows add-on](windows-mcp-additions.zip) and extract it alongside the
files from the original `remote-check.zip`. Keep the original wheel and MCP probe;
their gateway implementation is unchanged. Open `WINDOWS.md` after extraction, or [read it online](windows-mcp/WINDOWS.md).
Replace GATEWAY_HOST with your gateway host locally. The public add-on contains no
actual network address or machine-specific deployment paths.

The add-on contains Windows setup instructions, a shared MCP launcher, Codex and
Claude configuration templates, and the real OpenCode-session acceptance prompt.
It contains no keys, passwords, model files, state, logs or backups. Windows paths
are templates that must be adjusted to the local account and installation directory.

Actual Windows and remote-host acceptance remains pending. The current gateway MCP
selects/loads models and delegates bounded text requests; the coding client's own
terminal tools launch OpenCode. It has no dedicated OpenCode-session launch tool.

SHA-256: `869236f300e20e3c45513d0fbc5322adf4d91fbf0c3370091299c224ae1d41cc`
