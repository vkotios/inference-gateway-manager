# Maintenance lease acceptance

Run on 2026-10-21 against the branch code, a disposable fixture engine and a private lease
directory, on a separate port. The deployed gateway and real models were not touched.

A watcher-style script wrote leases by temp file and rename and read `status.json`:

- Acquire-then-verify: status showed the lease held with the writer's id within 3 s.
- While held: `POST /api/engine` and `POST /api/model` returned 423 with `Retry-After` and the
  holder and reason; unload stayed allowed.
- Operator `DELETE /api/maintenance` broke the lease; the id appeared in `revoked_ids` in the
  status file; renewing the same id stayed ignored; a new id was honoured.
- Release by deleting the file freed loads; the model loaded and `/v1` inference to a loaded
  model worked while a new lease was held.
- Unknown holder and malformed JSON were flagged `invalid` and did not block loads; a
  3-second lease expired on its own.
- A symlink planted at `status.json` was replaced by rename; its target was untouched.
- Revoked ids survived gateway restarts; SIGTERM stopped the gateway and its engine and freed
  both ports.

Not exercised: the dashboard banner in a real browser (static checks only; the Playwright
dashboard tests need a browser), and a lease against the deployed root gateway.

One harness finding, not a product defect: a native-engine matcher that matched the script's own
argument made the gateway refuse an engine switch as an unmanaged engine. The acceptance
configuration uses a matcher specific to the fixture backend.
