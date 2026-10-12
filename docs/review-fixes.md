# Review finding dispositions

These dispositions cover the 14 blocking findings from the completed Claude Sonnet 5.5/high review of the initial source snapshot. They describe fixes in the working tree, not approval of the corrected snapshot or changes to a running deployment.

| ID | Source resolution | Regression coverage |
| --- | --- | --- |
| S1 | Global executable/argv matches detect conflicts; they do not establish ownership. Require live launch ancestry, a verified surviving launch session, or a fresh launch tag for detached native children. Do not adopt a reused launcher PID or an unrelated replacement session. | `tests/test_review_lifecycle.py`: unrelated process during startup, tagged detached-child cleanup, exited-launcher PID reuse, dead-session reuse. |
| S2 | Strip all `IGM_` environment variables, including configured overrides, before launching a backend. Inject only the fresh `IGM_ENGINE_RUN_ID`; preserve ordinary backend options. | Backend `/proc` environment inspection in `tests/test_review_lifecycle.py`. |
| S3 | Track the outer ASGI response task through socket drain. Explicit force-stop cancels that task, closes the upstream stream, and releases admission before reload. | Stalled ASGI send, force unload, reload and subsequent inference in `tests/test_review_proxy.py`. |
| S4 | Compare UTF-8 credential bytes so a non-ASCII Authorization header returns 401 instead of an exception. | Malformed credential case in `tests/test_review_security.py`. |
| L1 | Retain the lifecycle lock until teardown actually finishes, even through repeated cancellation. Reserve pending-load admission before its internal task is scheduled. | Repeated cancellation with blocked cleanup, plus the competing-load scheduling-gap case in `tests/test_review_lifecycle.py`. |
| L2 | Refresh liveness for status/model listings; do not advertise a crashed model as loaded. When no owned process survives, reset without requiring an impossible native occupancy probe. | Crash, status, model listing and non-force reload in `tests/test_review_lifecycle.py`. |
| L3 | Convert failed process signalling to 503, retain owned identities and allow retry after the permission problem is resolved. | Injected permission denial and successful retry in `tests/test_review_lifecycle.py`. |
| L4 | Keep explicit force-unload available while a model-load request is pending. | Executed Node/VM behavioral dashboard test in `tests/test_review_dashboard.py`. |
| L5 | Match the Strata Python module invocation using `argv_contains: serve.server`, not an executable name that the example never launches. | Disposable matching process in `tests/test_review_config.py`. |
| L6 | Return 409 to a loader interrupted by force; successful cleanup leaves selected state without a misleading startup-failure error. | Concurrent HTTP load/force-unload in `tests/test_review_lifecycle.py`. |
| L7 | Permit explicitly configured HTTPS origins for a TLS reverse proxy; retain same-origin access and deny other origins. | Allowed HTTPS, refused foreign HTTPS and existing HTTP cases in `tests/test_review_security.py`. |
| L8 | Reject nonfinite numbers, float overflow and excessive nesting with 400 before inference admission. Limit nesting to 128 levels. | NaN, both infinities, overflow and deep nesting in `tests/test_review_proxy.py`. |
| L9 | Parse Host authorities structurally, including IPv6, and accept case-insensitive Bearer schemes. Reject foreign/userinfo hosts. | IPv6, lower-case Bearer and refused-host cases in `tests/test_review_security.py`. |
| L10 | Permit unload/reset after failed spawn. Document the distinct 409 lifecycle/shutdown refusals and 503 inference occupancy failures. | Failed spawn/unload/reload in `tests/test_review_lifecycle.py`, plus existing safety/admission controls. |

## Verification

The complete suite passed with **41 tests**. Ruff lint and formatting, JavaScript syntax checking, source-distribution/wheel builds, and `git diff --check` passed. Existing busy/unknown-occupancy refusal tests remain in the suite. The new ownership tests use disposable processes; the detached-child test models the ownership boundary rather than executing a real container engine.

Reproduce the source checks:

```sh
python -m pytest -q
ruff check gateway tests
ruff format --check gateway tests
node --check gateway/static/app.js
python -m build
git diff --check
```

## Deployment boundaries and remaining work

- No real backend or manager service was restarted to deploy these fixes.
- A fresh independent review of the corrected source is still outstanding; the earlier review does not approve these changes.
- Real-engine transitions, real-container teardown and remote LAN/TLS acceptance have not been rerun against this corrected build.
- A container launcher that loses ancestry/session must explicitly forward `IGM_ENGINE_RUN_ID={launch_id}` into the native process environment, as described in the README. A global matcher alone is deliberately insufficient.
- S3 provides explicit force-stop recovery. It does not introduce automatic slow-consumer eviction or an inference-generation deadline.
- S2 protects the gateway's `IGM_` credentials; it is not general sanitization of every inherited environment variable.
- Publication is separate and has not been performed by this fix pass.
