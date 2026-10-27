# Engine updates, verification and rollback

Engine updates are **opt-in** and disabled unless `engine_updates` is configured. They are independent of gateway package upgrades and the fresh-install workflow. This feature does not discover arbitrary package managers, install engines for you or run unattended upgrades. It supports administrator-owned GitHub source/build recipes for engines such as llama.cpp, Strata, Kyojin, ds4, Gufo, Lucebox and ds4a; each installation must be enrolled with its actual build/runtime layout.

## Operator workflow

Open **Engine updates and rollback** on the authenticated dashboard:

1. **Refresh update settings**, select the engine, then **Check for updates**. The gateway reads public GitHub release/tag metadata, resolves choices to immutable commit SHAs, and inspects the installed checkout's Git revision (or an administrator-declared version command for a packaged installation). An installation without useful Git metadata is reported as unknown, not assigned a guessed version. Checks do not stop engines or change installed files.
2. Select a version, then **Stage and check compatibility**. It fetches that exact commit into a new private release directory, runs only administrator-declared build commands, and checks the executable's help/version contract. Baseline directories and previous releases are not overwritten. Staging can consume CPU, disk and RAM: schedule large builds when capacity permits. It does not request a model load. Administrator recipes must not run GPU tests, change global dependencies or invoke privileged host setup during a build.
3. Review the **named test model(s) and context**. One designated model per engine is the normal default. Add a second test only when materially different profiles need coverage (for example no draft versus DSpark or a different artifact format). This is a compatibility smoke test, not a test of every configured model or maximum context.
4. Authorize the named model loads and short inference tests, then **Test and activate** during idle maintenance. The confirmation covers verification of the candidate and the previous version if automatic rollback is needed. The gateway refuses busy/unknown native occupancy, active requests, an existing maintenance lease, unmanaged engines, another update or a scheduled host recovery action. Once reserved, it blocks normal model loads, inference, registrations and recovery actions during the transaction. A new work arrival during the idle check causes refusal rather than interruption.
5. The updater stops the previous idle engine normally, loads each authorized test model at its declared modest context, checks an exact known-answer Chat Completions response and a complete streamed response, then verifies process exit. `/health` 200 alone never promotes a candidate. On success, the selected engine's launch configuration points to the staged version and is persisted as a private overlay. The gateway is left **unloaded**; it does not resume another model or interrupted work. Explicitly load the model you want next.

The existing `gateway_only` engines still have no verified native-slot occupancy. All inference must pass through IGM; direct loopback clients bypass admission tracking. Stop/reconcile external callers before maintenance. The updater cannot prove that an untracked caller is idle.

## Administrator enrollment

Merge a reviewed `engine_updates` object into the private host configuration and restart the gateway during authorized maintenance. Do not replace a live config with an example. The keys are engine IDs already present under `engines`; ds4 uses its existing `dwarfstar` ID and ds4a remains separate. See [the recipe example](../examples/engine-updates.example.json).

Each recipe declares:

- `repository`: an exact public GitHub `owner/repository` selected by the administrator. Clients cannot provide URLs, build commands, paths or test prompts.
- `installed_root`: the dedicated baseline engine directory. It must be separate from approved `model_roots`. Keep weights outside engine installations. The updater retains the original root and never updates it in place.
- Optional `version_argv`: a read-only administrator-owned `--version` command, using `{installed}` for the baseline root. When no installed Git revision is available, the checker extracts a bounded semantic version from its output. A missing/unsupported version probe remains unknown. No model load is requested.
- `channel`: `releases` (default, excludes drafts/prereleases), `tags`, or explicitly `branch` with a `branch` name. Branch mode offers a clearly labeled commit snapshot. Only the first ten release/tag choices are shown. Public GitHub rate limits apply; failures do not trigger retries or local changes. There is no GitHub credential storage.
- `build`: nonempty argv arrays. Commands have absolute executables (or `{release}/...` executables) and run with the staged release as cwd. `{release}` is substituted in arguments and `build_env` values. No interactive shell activation or arbitrary client command is supported; a reviewed, administrator-owned build script can encapsulate complex engine-specific steps.
- `checks`: argv commands plus required `contains` strings, typically `--help`/`--version` checks proving required flags still exist. These are rerun before switching. The staged executable checksum and recipe/config fingerprint must still match. Help text alone is insufficient; actual inference is checked separately.
- `executable`: the relative staged executable path matching `argv[0]` of each designated test model. Native executables must stay confined to the release. A release-local Python venv interpreter may link to its system Python, but requires its own `pyvenv.cfg`; keep its packages inside that release. Global Python/ROCm/CUDA drivers are prerequisites, not updated or rolled back by this feature.
- `tests`: normally one `{model, context, expected}` entry. `model` is an enabled static or registered gateway model ID; `context` is 1,024–32,768 tokens and must fit the model's declared limits. `expected` is a short uppercase token such as `IGM_UPDATE_OK`. The test asks for that token, allows at most 64 output tokens and validates non-streamed content, streaming content and `[DONE]` termination. Models that cannot follow this short check need an appropriate reviewed test configuration; a failure may indicate behavior rather than binary startup incompatibility.

The example is a gfx1151 HIP source build, not a universal or GPU-qualified recipe. Review prerequisites and the installed upstream revision before enabling it.

The updater rewrites paths rooted at the active engine root in launch argv, cwd, environment and generated engine configuration templates to the new release. Model files outside that root remain untouched. Review all profiles, external tools and dependencies when enrolling: a different engine layout, renamed binaries or changed flags requires an explicit recipe/config change, not a guessed automatic migration. A successful canary does not certify other models or APIs such as Responses, Messages or tool calls.

Use a **separate venv/build directory for every release**. Build recipes must never run `sudo`, install into a global Python environment, modify drivers, change clocks/memory limits or write into the baseline installation. Recipes and upstream build code are trusted executable configuration, not a security sandbox. Build subprocesses receive a filtered environment without inherited gateway keys/tokens/watchdog settings; explicitly declared build variables are administrator-owned. Runtime snapshots omit inherited credential-like variables; explicitly configured engine environment values are retained. These rollback snapshots are private and may include sensitive configured values; protect the state directory like credentials.

## Rollback and interruption recovery

If candidate verification fails, the gateway restores its previous executable/root bindings, environment and engine configuration, then verifies the prior version using the same authorized test selection. The previous executable/dependency directories remain intact; model weights are not copied or modified. If restoration verification also fails, it stops its owned test process where possible and **blocks inference and ordinary lifecycle actions**. Fix the underlying problem and use **Roll back and verify** for the engine requiring recovery. Another engine's successful test cannot clear that recovery requirement.

The **Roll back and verify** action remains available after a successful upgrade for regressions discovered later. It restores the most recent retained prior snapshot for that engine, verifies it and leaves the gateway unloaded. A failed manual rollback restores the version that was active before the rollback attempt. Neither path forces somebody else's active request or unmanaged process to stop.

Transactions are journaled before stopping an engine. A gateway restart with an incomplete journal restores the prior configuration overlay, suppresses startup defaults and requires explicit recovery verification. It does not silently load a test model during startup. Surviving unmanaged processes must be reconciled by their owner before verification.

If the gateway cannot start, stop its real supervisor and use the offline recovery command with the **same private state directory**:

```sh
.venv/bin/python -m gateway rollback-engine-config \
  --state-dir /srv/igm-state --engine lucebox
```

This refuses a running gateway's lock, restores the previous configuration pointer, and starts no processes. Start the gateway, then authorize **Roll back and verify**. If private base configuration was edited externally after enrollment, restore its compatible backup first: the gateway refuses to silently overwrite changed administrator settings with a stored overlay. The original private config is never rewritten by the updater. Removing an update recipe disables future update operations; it does not discard the installed version overlay or roll back implicitly. Keep backups of both it and the state directory; engine overlays are part of the installed state, not disposable cache.

Private state is under `STATE_DIR/engine-updates/`. It retains bounded receipts (the latest 30 plus necessary rollback snapshots), staged source/build directories, original environment/config snapshots and pending-transaction metadata. Release directories are deliberately not automatically deleted: monitor disk usage and prune only versions no longer referenced by installed, staged, pending or rollback records, while the supervisor is stopped. Do not delete a retained venv/executable and expect rollback to recreate it. Use separate cache/save directories per release, or disable persistent caching for the designated test: shared caches outside an engine root are not snapshotted or schema-migrated. Global driver/package changes or separately modified/deleted baseline directories are outside rollback's guarantees.

## Optional GitHub problem reports

After an update failure or user-triggered rollback, **Review problem report** prepares an editable local draft. Its allowlist includes gateway/engine revisions, operation/failure step, verification/rollback outcome, modest test context, OS/architecture/kernel/Python version, GPU PCI vendor/device IDs and runtime version strings recognized from compatibility checks. Unknown version information stays unknown. It omits raw logs, command arguments, environment values, credentials, model names, filesystem paths, hostnames and prompts/responses.

Review/edit it before choosing **Open GitHub draft**. GitHub issues are public. Opening a prefilled draft does not submit an issue: the user signs in to GitHub and submits it there. The gateway stores no GitHub token, does not send diagnostics automatically and does not require reporting to complete rollback. Long URLs or restricted browser environments may require copying the reviewed text into a new issue manually.

The report establishes which smoke checks passed/failed; it is not a model benchmark or proof of every engine/model workflow. Include any additional reproduction details deliberately, taking care not to paste private data.

## API and validation

All update routes require the control role and the existing browser CSRF protections:

| Method | Route | Behavior |
| --- | --- | --- |
| GET | `/api/updates` | Configuration, installed revision, staged versions, job and recovery state |
| POST | `/api/updates/ENGINE/check` | Read public metadata; no engine mutation |
| POST | `/api/updates/ENGINE` | `{action: "stage", revision: "COMMIT_SHA"}` or `{action: "apply", revision: "COMMIT_SHA", test_models: ["MODEL_ID"]}` or `{action: "rollback", test_models: ["MODEL_ID"]}` |
| GET | `/api/updates/report` | Local report preview and prefilled GitHub draft URL; sends nothing |

Mutation requests return 202 for a job; poll `/api/updates` for completion or refusal/failure. HTTP acceptance does not mean an engine was upgraded. Test authorization must name exactly the recipe's designated test model IDs. Stage selection is restricted to discovered immutable revisions. Public catalog/status responses do not expose executable paths, environment snapshots or raw command errors.

Repository tests use disposable HTTP engines and a tiny local Git source, never real GPU inference. They cover wrong-answer and broken-stream rejection, rollback verification, restart/lock handling, staging/checksum/recipe constraints, busy/lease admission, report privacy and browser consent/review. Real builds, runtime dependencies, supported models and GPU/driver behavior require separate target-host acceptance.
