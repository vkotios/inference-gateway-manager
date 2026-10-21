# Maintenance Lease Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox tracking.

**Goal:** Let a co-located job runner pause gateway-mediated model loads through a private
lease directory, with an operator override.
**Architecture:** A small `gateway/maintenance.py` owns directory validation, lease
parsing, revocation persistence and the status writer. `app.py` gates the two load
endpoints and adds `/api/maintenance`; `core.py` rechecks inside the lifecycle lock.
**Spec:** docs/superpowers/specs/2026-10-21-maintenance-lease-design.md

## Global Constraints

- Preserve existing API bodies; absent config changes nothing.
- Fail open on every invalid lease; never gate unload, registration or inference.
- No root-style file hazards: no-follow opens, ownership/mode checks, rename-into-place.
- Test first; run the complete suite and ruff before completion.

### Task 1: Lease core
**Files:** gateway/maintenance.py, gateway/config.py, tests/test_maintenance.py
- [ ] Failing tests: config validation, directory checks, lease parsing and refusals.
- [ ] Implement Maintenance (directory open, read_lease, revoked persistence, status write).
- [ ] Config validation for the `maintenance` block.

### Task 2: Gateway integration
**Files:** gateway/app.py, gateway/core.py, tests/test_maintenance_api.py
- [ ] Failing tests: 423 + Retry-After, lock-time recheck, break/revoke, status object,
      status file refresh, startup default load not gated.
- [ ] Implement gate, endpoints, status loop, manager recheck.

### Task 3: Dashboard, docs, acceptance
**Files:** gateway/static/app.js, style.css, docs/maintenance-lease.md, README/features
- [ ] Banner and Break button; documentation; isolated-gateway run with a real writer.
- [ ] Full suite, ruff, build; PR on its own branch.
