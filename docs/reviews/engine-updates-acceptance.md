# Engine update workflow: repository acceptance

This change is separate from PR #7's fresh installer and builds on main after PR #8. It adds opt-in administrator source/build recipes, a dashboard and control API for version checks/staging, explicitly authorized test-model activation, retained-version rollback, interruption/offline recovery and optional public GitHub issue drafts.

## Evidence

- Full suite: **309 passed**. The new updater coverage includes a tiny local Git repository, immutable source staging, executable/recipe checks, normal activation and manual rollback, wrong-answer and incomplete-stream rejection, verified automatic restoration, failed-restoration admission blocking, native-occupancy/lease refusal, concurrent API exclusion, interrupted transaction recovery, stopped-supervisor offline rollback, private journal permissions, report allowlisting and registered-model profile replay after restart.
- After the final inherited-credential snapshot filter, focused updater/documentation verification: **36 passed**. Installed versions can come from Git metadata or an optional administrator-owned read-only version command; unknown remains unknown.
- Chromium coverage verifies the named-model authorization checkbox and confirmation payload, an editable report linking only to a GitHub draft, narrow-screen layout and report clearing on sign-out. It never submits an issue.
- Ruff lint/format, whitespace checks, both JavaScript syntax checks and source/wheel packaging passed. CI also checks the new dashboard script's syntax.

## Boundaries

Tests used temporary files, local repositories and disposable HTTP processes, not model benchmarks. No real engine source release was built or GPU model loaded. The included Lucebox HIP recipe requires administrator review for the exact installation, upstream revision, compiler/runtime and hardware. Other engines use the same recipe mechanism but still need their own reviewed build contracts.

Builds are trusted code, not sandboxed package-manager transactions. Previous release directories, separate venvs and configuration overlays must be retained. Global driver/package changes and shared external cache schemas are outside automated rollback guarantees; use separate release-local cache/save paths or disable persistence for the test profile. Gateway-only occupancy does not detect direct native callers. A successful designated canary is not certification of every model, context, API or tool-call workflow.

No host model files, installed engines/services, live gateway configuration or host `.local` artifacts were changed. No GitHub diagnostic issue was sent. Runtime configuration/environment snapshots stay private; the public report uses an explicit allowlist and requires user review and GitHub submission. The feature defaults to disabled and starts no models merely because it is configured or checked.
