# Gateway Controls Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox tracking.

**Goal:** Implement the five capabilities approved in the accompanying design.
**Architecture:** Shared launch settings and model registry feed the existing manager/API.
Independent browser-auth and MCP modules retain the existing process and admission boundary.
**Tech Stack:** Python 3.11, FastAPI/httpx, SQLite/Argon2id, official MCP SDK, plain JS/CSS.
**Spec:** docs/superpowers/specs/2026-10-20-gateway-controls-design.md

## Global Constraints

- Preserve existing API bodies, engine ownership and single-request admission.
- No live-service changes, arbitrary user commands, implicit force, conversion or downloads.
- Client reasoning wins; explicit HTTP session opt-in; CLI credentials from environment.
- Use test-first behavior tests and check complete suite before completion.

## Review Focus

- Validate all settings before a running backend is stopped.
- Freeze loaded configuration across registry mutations and reject stale selections.
- Constrain symlinks and every companion artifact to configured roots.
- Never let a session cookie authenticate inference or bypass CSRF with bad bearer headers.
- Keep Linux-only imports out of the stdio adapter on non-Linux hosts.

### Task 1: Shared settings, registration, Gufo

**Files:** gateway/settings.py, gateway/registry.py, gateway/core.py, gateway/app.py,
gateway/config.py, examples/config.example.json, tests/test_settings.py,
tests/test_registry.py, tests/test_gufo.py.
**Interfaces:** POST /api/model optional engine/context/reasoning; status active_settings,
selection_id; model catalog context_limits/reasoning_modes/reasoning_change;
GET /api/model-files, POST /api/models/inspect, POST /api/models.
- [x] Write and run failing settings, lifecycle, registry and Gufo fixture tests.
- [x] Implement validated administrator launch profiles, frozen active spec and transitions.
- [x] Implement confined artifact inspection and atomic data-only registry writes.
- [x] Expose additive APIs/catalog, Gufo example, and pass focused/full fixture tests.
- [x] Review settings/registration trust boundaries and record verification.

### Task 2: Password and persistent sessions

**Files:** gateway/browser_auth.py, gateway/app.py, gateway/__main__.py,
tests/test_browser_auth.py, pyproject.toml.
**Interfaces:** /api/auth/login, /session, /logout, /sessions; CLI set-password.
- [x] Write and observe failing authentication/expiry/CSRF/role/reset tests.
- [x] Implement Argon2id, private SQLite, opaque hashed sessions, revocation and throttling.
- [x] Integrate with existing network/Host/Origin middleware and API bearer roles.
- [x] Add local password CLI; verify reset and full regressions.

### Task 3: Dashboard controls and registration

**Files:** gateway/static/index.html, app.js, style.css, DESIGN.md,
tests/test_dashboard_browser.py.
**Interfaces:** Consume Tasks 1/2 APIs; retain old control-key login as advanced option.
- [x] Add password/remember/restore/logout/session revocation UI.
- [x] Add capability-driven context/reasoning controls, active settings and reload notice.
- [x] Add root-confined file picker, inspect/review/add flow and empty/error states.
- [x] Verify keyboard, narrow screens and browser interactions; retain force confirmations.

### Task 4: CLI, remote MCP and documentation

**Files:** gateway/client.py, gateway/mcp.py, gateway/__main__.py,
tests/test_remote_client.py, tests/test_mcp.py, docs/features.md, README.md.
**Interfaces:** HTTP client with fixed origin/no redirects; optional MCP extra and igm-mcp
entrypoint. Tools catalog/status/telemetry/load/unload/delegate with selection guard.
- [x] Write and observe failing client/fragment/delegation/stdio tests.
- [x] Add registration CLI and Hermes/OpenCode fragment export without secrets.
- [x] Implement official SDK stdio adapter and bounded delegation, no Linux imports.
- [x] Document all new config and migration steps, then verify full suite/lint/build/JS.
- [x] Perform independent whole-change review and fix actionable findings.

## Implementation verification

Final repository run: **173 passed** (26.65s), including Chromium workflows and real
MCP stdio exchange with disposable fixture processes. Ruff lint/format checks, JavaScript
syntax, whitespace checks, strict UI audit and source/wheel builds passed. Built wheel
modules and dashboard assets match the working tree. Independent backend and final
integration reviews have no remaining actionable findings after regression-tested fixes.

The feature branch is `codex/gateway-controls`. No deployed
configuration, service or real model was changed. Actual engine/GPU compatibility,
client endurance and second-host acceptance remain the documented deployment checks.

## Opus findings follow-up

- [x] Reproduce and fix unavailable-registration startup failures; add safe API/CLI removal.
- [x] Verify Gufo slot and CLI contracts against hashed upstream source; withdraw incorrect schema concern.
- [x] Preserve omitted settings on repeated loads of the same engine/model.
- [x] Move browser session storage outside the event loop, batch activity writes and return safe storage errors.
- [x] Retain/document bounded login limits and verify operator recovery during throttling.
- [x] Add Linux Python 3.11/3.12 and portable Windows/macOS CI coverage.
- [x] Verify 192 tests pass on each local Python version; lint/build/UI checks pass.
- [x] Obtain Opus correction-review signoff, including final minor follow-ups.

See [finding validation and evidence](../reviews/2026-10-20-opus-findings-validation.md).
