# Maintenance lease

A job runner on the same host can pause **model loads** while it uses the machine's shared
memory, for example a CI virtual machine on a host whose GPU and RAM are one pool. The
runner holds a lease; the gateway refuses `POST /api/model` and `POST /api/engine` until it
ends. Nothing is unloaded or stopped, loaded models keep serving, and the operator can
always break the lease.

It is off unless `maintenance` is configured. It covers loads made through the gateway
only; engines started by hand are not affected.

## Configuration

```json
"maintenance": {
  "dir": "/home/me/.local/state/igm-maintenance",
  "holders": ["procurementmind-ci"],
  "max_ttl_seconds": 5400
}
```

The runner must run as the same Unix user as the gateway. The gateway creates `dir` with
mode 0700. It refuses the feature, and writes nothing, if the directory is a symlink, is not
owned by the gateway user or is accessible to group or others. Lease files must be regular
files owned by that user, no wider than mode 0600, at most 4 KiB, and are opened without
following symlinks.

## Lease file (written by the runner)

`<dir>/lease.json`, replaced atomically (write a temp file, rename):

```json
{"version": 1, "id": "0b1c2d3e-4f50-4a6b-8c7d-9e0f1a2b3c4d", "holder": "procurementmind-ci",
 "reason": "CI run 123", "acquired_at": 1800000000, "expires_at": 1800000600}
```

`id` is 8-64 characters of `A-Za-z0-9_-`, `reason` at most 200 printable characters, times
are epoch seconds, and `expires_at` may be at most `max_ttl_seconds` ahead. Renew by
rewriting with the same id and a later `expires_at`; release by deleting the file. The lease
**fails open**: an unknown holder, a malformed or oversized file, extra keys, a bad mode, a
too-distant expiry or a revoked id is ignored, logged, and reported as `invalid` in the
status. An expired lease is simply not held.

## Status file (written by the gateway)

`<dir>/status.json`, mode 0600, rewritten at least every 2 seconds and after every
load/unload/engine/break action:

```json
{"version": 1, "updated_at": 1800000001.2, "phase": "ready", "loaded_model": "qwen",
 "active_requests": 0, "native_busy": false, "lifecycle_busy": false,
 "lease": {"held": true, "id": "...", "holder": "...", "reason": "...", "expires_at": 1800000600,
           "invalid": false, "problem": null},
 "revoked_ids": []}
```

Field names are stable; `version` is the compatibility switch, so a reader should refuse a
version it does not know. Treat `updated_at` older than about 15 seconds as a stopped
gateway. The file is replaced by rename from a fresh, exclusively created temp file, so a
planted symlink is replaced and never written through.

## While a lease is held

`POST /api/model` and `POST /api/engine` return **423** with a `Retry-After` header and
`Model loads are paused by '<holder>' until <UTC time> (<reason>)`. The check runs again
inside the lifecycle lock before any running engine is stopped. Unload, registration, status
and inference to an already-loaded model are unaffected, and so is loading the configured
default model at gateway start.

## Operator override

- `GET /api/maintenance` returns `{enabled, error, lease, revoked_ids}`; `/api/status`
  carries the same object as `maintenance`.
- `DELETE /api/maintenance` (control credential or signed-in browser) breaks the held lease.
  Its id is added to `revoked_ids` (kept in the state directory, newest 64) and ignored even
  if the runner keeps renewing it. The gateway does not delete the runner's file. 404 when the
  feature is off or nothing is held.
- The dashboard shows a banner with the holder, expiry and reason, and a **Break lease**
  button.

## Runner protocol

1. Read a fresh `status.json`: `phase` is not `loading`, `lifecycle_busy` false, no active
   requests, plus the runner's own memory checks.
2. Write `lease.json`, wait past one status refresh (2-3 s), read `status.json` again and
   require `lease.held` with your id; otherwise delete the lease and back off.
3. Renew about every 5 minutes. If the lease disappears or your id appears in `revoked_ids`,
   stop the job promptly and acquire a **new** id later.

The gateway cannot estimate a model's memory, so it has no minimum-free-memory setting; the
runner keeps its own policy. Any process running as the gateway's Unix user can write a lease;
the holder allow-list, TTL cap and fail-open rules bound the effect to a temporary load pause.
