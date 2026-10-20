# Opus review findings: validation and corrections

Scope: the six findings from the independent Opus 5.5 review of the uncommitted
`codex/gateway-controls` implementation against `22cae7f`. No deployed service,
configuration or real model was changed.

| Finding | Validation | Resolution |
|---|---|---|
| F1: unavailable registrations prevent startup | Reproduced with missing files, roots, profiles, engines and a static-ID collision | Invalid registrations become disabled catalog entries where possible and appear in `unavailable_registrations`. Static models still run. Authenticated API/CLI removal changes only saved registry data; loaded models must be unloaded first. |
| F2: Gufo slot shape incompatible | Not supported by the upstream implementation | Gufo explicitly emits `is_processing` and `n_ctx`; its own contract tests require both. Added complete source-derived idle/busy fixtures exercising readback, streaming, refusal and ordinary unload. No speculative adapter added. |
| F3: omitted settings reset a loaded model | Reproduced with a disposable subprocess; repeated old-style load replaced its process | A matching engine/model inherits each omitted field from its selected launch settings. Explicit changes still apply, and other models use their own defaults. |
| F4: global/proxy login throttle can deny new logins | Confirmed as an availability tradeoff of the single-operator account | Retained bounded global/per-source limits and two concurrent verification workers. Documented shared proxy identity and control-key recovery. Tested that existing sessions and control-key access remain usable during throttling, and login resumes after the time window. |
| F5: session SQLite calls block the API event loop | Reproduced using a real exclusive SQLite lock on status and session restore | Session/configuration/list/revoke calls run in worker threads. Activity touches are conditional and limited to once a minute. Health continues while the database is locked; expiry, reset and revocation regressions remain covered. |
| F6: Python/OS coverage gap | CI used only Ubuntu/Python 3.12 | Added full Linux runs on Python 3.11 and 3.12, plus portable client/MCP tests on Windows and macOS. Only the fixture that starts the Linux gateway itself is skipped on other OSes. |

## Gufo source verification

Checked upstream revision `7b2784517f5652860e03c4ff0c61f2d8940139db`:

- [LlamaSlots emits both fields](https://github.com/gufo-org/gufo/blob/7b2784517f5652860e03c4ff0c61f2d8940139db/src/cli/serve/http_server.cpp#L1448-L1490).
- [Gufo's slot conformance test asserts both fields](https://github.com/gufo-org/gufo/blob/7b2784517f5652860e03c4ff0c61f2d8940139db/tests/functional/server_metrics.py#L135-L167).
- [The LLM CLI explicitly accepts served-model-name and context](https://github.com/gufo-org/gufo/blob/7b2784517f5652860e03c4ff0c61f2d8940139db/src/cli/serve/serve.cpp#L1167-L1187).

The prose server document abbreviates the slot fields. A response constructed only from
that abbreviated description does not reproduce the current implementation. Actual
installed-build/GPU verification remains a deployment check; this validation does not
claim to have launched Gufo.

## Regression evidence

Tests were observed failing before the corresponding fixes for F1, F3 and F5. Additional
coverage verifies that removal retains weights and static configuration, refuses loaded
models, and completes a durable removal before releasing a canceled writer's lock.
Structurally corrupt/unreadable registry data still fails closed instead of being lost
or overwritten. Removal does not become an MCP tool and grants no filesystem deletion.

## Verification and correction review

The initial correction set passed **186 tests on Python 3.11** and **186 on Python 3.12**.
Opus 5.5 independently re-ran 55 focused tests and the original reproductions, signed off
on the corrections, and withdrew F2. Its minor follow-ups were addressed as follows:

- Session-store failures now map to a safe 503 across asynchronous authentication paths,
  with real read/write-lock regression coverage. Health and bearer control remain usable.
- Startup logging identifies the unavailable registration and bounded validation reason,
  without logging artifact paths.
- Removal deliberately holds the lifecycle lock through persistence to prevent load races;
  the ordinary transient lifecycle-busy 409 is documented instead of weakening that guard.

Gufo source files were retrieved by the full commit ID above and checked against their
Git blob hashes. The Git commit object explicitly identifies its tree as
`769e30ecd5eb6597ce14e6b639a76f0fe65bff1c`. These files are byte-identical to the earlier
revision the reviewer read. The provenance record distinguishes commit, tree and blob
identifiers without claiming hardware execution.

Final verification after the timeout and logging follow-ups:

- Python 3.12: **192 passed** (29.26s).
- Python 3.11: **192 passed** (29.17s), using a disposable environment.
- Ruff lint/format, JavaScript syntax, whitespace checks, strict UI audit and package
  build passed. The wheel's implementation modules and assets match the working tree.
- Opus independently verified the final timeout and diagnostic changes with 11 targeted
  tests and a real five-second database-lock probe; no blockers remained. Its suggested
  provenance tree-label correction was applied and checked against the Git commit object.

Hosted Windows/macOS jobs have not run from this checkout. Actual engine/GPU execution,
Hermes configuration acceptance and second-host MCP remain deployment acceptance checks.
At validation time, changes were uncommitted on `codex/gateway-controls`; no service or model was started
outside disposable test fixtures.
