# Gateway controls design

Approved in chat on 2026-10-20; implementation requested explicitly.

Keep explicit engine/model loading, a single engine and admitted request, identity-based
teardown, separate bearer credentials, and existing administrator-owned launch commands.
Do not modify the deployed configuration or run actual models during development.

## Shared settings

One stable model registration supports validated context and reasoning defaults. Extend
POST /api/model with optional engine, context, reasoning (auto/on/off); old bodies work.
Validate before stopping; context changes restart; identical settings do not. Client
reasoning controls win over defaults. Capability declarations are per model/template;
fixed or unknown models cannot claim a toggle. Read effective per-session context from
native metadata when available. Never equate client budgets, output limits and KV capacity.
Return active_settings and selection_id; optionally reject stale inference selection IDs.

## Browser authentication

Single operator password, Argon2id hash, local set/reset CLI. Persistent hashed opaque
sessions in private SQLite; remember: 30 days absolute/seven days idle, normal: 12 hours.
HttpOnly host-only SameSite=Strict cookies, CSRF on cookie-authenticated mutations, exact
Origin checks, rate limiting, reset/logout/revocation. Bearer API access remains intact;
cookies never authenticate /v1. HTTPS secure cookies by default; LAN HTTP explicitly
enabled with allow_insecure_http_sessions, documenting interception exposure.

## Registration and Gufo

GUI and authenticated CLI share host-side inspection and registration APIs. Restrict
filesystem access to configured roots; metadata only, never execute artifact code. Support
GGUF/split GGUF and known engine packs with companion checks. Runtime commands and
templates remain administrator owned; persisted registry holds validated data only.
Atomic writes, duplicate refusal, immediate catalog refresh, no automatic loading or
conversion/download. Legacy static entries remain supported.
Gufo is fifth engine, loopback 8083, /ready, native /slots, served model name matching ID,
one session, text workflows first, speculative features optional.

## Remote MCP

Optional cross-platform stdio adapter on harness host, fixed gateway URL and environment
credentials. Discover, status, telemetry, explicit load/settings, ordinary unload, bounded
prompt delegation. No force/recovery/arbitrary commands/registration tools. Delegation
returns text and never independently acts on harness files. Standard provider configuration
still supplies a harness's main model. Include Hermes/OpenCode fragment export.

## Acceptance

Fixture lifecycle, settings/metadata, reasoning precedence, sessions/CSRF/roles, confined
registration/persistence/concurrency, Gufo slots/SSE, and real stdio MCP exchange. Verify
full tests/lint/build/JS plus dashboard interactions. Real GPU/client and second-host
acceptance remains a documented deployment gate.
