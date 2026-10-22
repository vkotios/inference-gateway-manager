# Maintenance lease design

Approved in chat on 2026-10-21 by the operator; implementation requested explicitly.

A co-located job runner (the ProcurementMind CI watcher, which runs a VM on the same
host) needs the host's shared memory without a model load starting underneath it. It
holds a lease; while the lease is held the gateway refuses to start loads. It never
stops inference, unloads, or takes a credential.

## Constraints

- No new listener and no new credential. Both directions use a private directory owned
  by the gateway's Unix user (the watcher runs as the same user).
- Fail open: an invalid, expired, unknown-holder or over-long lease is not held. A crashed
  or buggy watcher can never block inference or the operator.
- The operator always wins: the control credential or browser session can break a lease.
- Only gateway-mediated loads are covered. Engines started by hand are not.
- Absent `maintenance` config means the feature is off and nothing changes.

## Configuration

    "maintenance": {"dir": "/home/me/.local/state/igm-maintenance",
                    "holders": ["procurementmind-ci"], "max_ttl_seconds": 5400}

`dir` absolute; `holders` non-empty list of `[A-Za-z0-9_.-]{1,64}`; `max_ttl_seconds`
integer 60..86400 (default 5400). The gateway creates the directory with mode 0700 when
missing. At every access the directory is opened without following a final symlink and
must be owned by the gateway uid with no group/other permissions; otherwise the feature
reports an error, no lease is honoured and nothing is written.

## Lease file (watcher writes)

`<dir>/lease.json`, written by temp file and rename:

    {"version":1,"id":"<8-64 chars [A-Za-z0-9_-]>","holder":"procurementmind-ci",
     "reason":"<=200 printable chars","acquired_at":<epoch s>,"expires_at":<epoch s>}

Renew by rewriting with the same id and a later `expires_at`; release by deleting the
file. The file must be a regular file owned by the gateway uid, mode no wider than 0600,
at most 4 KiB, and is opened without following symlinks. Held requires: all keys present
and no others, `version` 1, holder allow-listed, `acquired_at < expires_at`,
`expires_at > now`, `expires_at - now <= max_ttl_seconds`, id not revoked. Expired is
simply not held; the other failures are reported as `invalid` with a short reason.

## Status file (gateway writes)

`<dir>/status.json`, mode 0600, written to a fresh temp file inside the directory (no-follow,
exclusive create) and renamed over `status.json`, so a pre-planted link is replaced and never
written through. Rewritten at least every 2 s and immediately after a lease change:

    {"version":1,"updated_at":<epoch>,"phase":"unloaded|selected|loading|ready|error",
     "loaded_model":null|"id","active_requests":0,"native_busy":false|true|null,
     "lifecycle_busy":false,
     "lease":{"held":false,"id":null,"holder":null,"reason":null,"expires_at":null,
              "invalid":false,"problem":null},
     "revoked_ids":["..."]}

Field names are stable; `version` is the compatibility switch. A reader must treat
`updated_at` older than about 15 s as a stopped or stale gateway.

## Enforcement

While a lease is held, `POST /api/model` and `POST /api/engine` return HTTP 423 with
`Retry-After` (seconds to expiry, at least 1) and detail
`Model loads are paused by '<holder>' until <ISO-8601 UTC> (<reason>)`. The check runs in
the API handler and again inside the lifecycle lock before the running engine is stopped.
Startup default-model loading is not gated. Unload, registration, status, telemetry and
inference to an already-loaded model are unaffected.

## Operator override

`GET /api/maintenance` returns `{enabled, lease, revoked_ids, error}`. `DELETE
/api/maintenance` breaks the held lease: its id is recorded as revoked (persisted in the
state directory, capped at 64 entries) and is ignored even if the watcher keeps renewing
it. The gateway does not delete the watcher's file. 404 when disabled or when no lease is
held. `/api/status` gains a `maintenance` object. The dashboard shows a banner with the
holder, expiry and reason, and a Break button.

## Watcher protocol (informative)

Acquire-then-verify: read a fresh status (phase not loading, no active requests), write
the lease, wait past one status refresh, re-read and require `lease.held` with the same
id, otherwise delete and back off. Renew about every 5 minutes. Stop the job when the
lease vanishes or its id appears in `revoked_ids`, then acquire a new id later.

## Acceptance

Unit tests for parsing, permissions and symlink refusal, status writes, 423 and
Retry-After, lock-time recheck, break/revoke persistence and config validation; an
isolated-gateway run with a real watcher-style writer; full test and lint pass.
