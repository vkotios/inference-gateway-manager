# PR #7 installer review

Reviewed the original installer commit `0e637071002aea6de795cd3e39ad5d586649a3c8` against `main` after PR #8 (`4e321ee5c93d546d99aa37483b40e67b2e154dd9`). PR #8 was merged into the installer branch before this revision.

## Findings addressed

| Priority | Original issue | Resolution |
| --- | --- | --- |
| P1 | Copy fallback copied private files, then recursively removed destination `.local`, `.venv` and `.git`; an accepted reconfiguration could lose credentials/state. | Fresh-install-only target checks, public-file allowlist, no destructive fallback, refusal of existing config/state/environment and separate nonempty destinations. |
| P1 | EOF accepted default answers, including installation/service-start confirmation. | EOF cancels; confirmation defaults to no; no automatic service start. |
| P2 | Five hardcoded engine IDs and a separate matcher table omitted Lucebox/ds4a and could drift from the public catalog. | Choices follow the example configuration; ds4's `dwarfstar` ID and ds4a's DSpark profile/context presets are preserved. All conflict matchers remain even for unselected engines. |
| P2 | Recovery options were offered with a user unit although the fixed recovery helper targets the system manager. | Recovery prompts only in prepared system-service mode; user/manual modes disable them. Helper and sudoers setup stays an explicit administrator step. |
| P2 | Service could start against placeholder model paths; the default target needed `/opt` write access. | User-writable default target, disabled sample models, no startup default, explicit registration/loading and no automatic start. |
| P2 | Setup did not configure LAN access or show the selected remote connection URLs. | Interactive bind IP, CIDR subnet and optional hostname; validated restricted defaults; final root and `/v1` URLs and credential-role guidance. |
| P2 | Reconfigure prompt promised changes but generator refused any changed existing config after package files were already replaced. | No reconfigure promise; existing installations are refused before mutation; manual gateway upgrade/rollback requirements documented separately. |
| P2 | Path prefix rewriting could change `/opt/models-old`, and shell substitution did not safely handle service paths. | Directory-boundary rewriting, absolute-path checks, quoted systemd paths/specifier escaping and exclusive unit/config creation. |
| P2 | Optional host MCP installation added an unused config section and obscured the separate-client architecture. | Host MCP option removed; client setup and existing-key guidance linked explicitly. |

## Confirmed onboarding decisions

The user requested interactive LAN settings and system-service setup with administrator commands. The installer remains unprivileged: it prepares the system unit for the actual invoking account, prints commands, and neither invokes sudo nor installs/starts the system unit itself. Manual startup remains the default. User units remain optional; their recovery flags are disabled.

Existing installations are preserved by refusing an installer rerun against their private config, state or environment. Automated gateway/engine update functionality is outside this PR and is a separate design discussion.

## Validation

- Full suite: 315 tests passed, including 38 installer tests.
- Ruff lint/format for `gateway`, `tests` and `installer`; shell and JavaScript syntax; source/wheel build passed. CI now includes installer lint/format checks.
- Scripted fresh onboarding covered manual, user and prepared system modes using fake pip/systemctl commands, real config generation and real private-key creation in disposable directories. Additional cases exercised `all`, existing-install refusal, EOF/cancellation, invalid networks, no-overwrite and symlink refusal.
- A clean temporary venv installed the built wheel and real dependencies. A disposable loopback gateway returned health 200 and an authenticated catalog containing `dwarfstar`, `lucebox` and `ds4a`, with every example disabled and no model loaded. The process was stopped after verification.

No host model files, live gateway configuration, existing services or `.local` host artifacts were changed. No GPU inference, real system-unit activation, sudo permissions, reboot or second-device LAN acceptance was tested.
