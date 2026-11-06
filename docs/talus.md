# Talus integration

IGM treats Talus as an administrator-configured native process, using the existing lifecycle, authenticated catalog and transparent inference proxy. The example offers `llama`, `strata`, `kyojin`, `dwarfstar`, `gufo`, `lucebox`, `ds4a`, `halogen`, `halogen-flash`, `talus` and `strixite`; the dashboard renders that catalog dynamically. The gateway runtime needs no engine-specific Python or JavaScript adapter.

## Supported example

Talus is an open-source Strix Halo engine (a fork of Strata) that can serve Qwen3.8-Flash-Next, the dense Qwen3.8 27B, GLM, MiMo and Laguna models from **GGUF** files:

- Engine ID: `talus`; model ID: `qwen3.8-flash-next-talus`.
- Launch: native binary `/opt/talus/bin/talus-server` bound to `127.0.0.1:8088`.
- Loopback backend: `http://127.0.0.1:8088`.
- Readiness: `/health` returns success after model load and warm-up.
- Occupancy: `probe: slots`, using `/slots` and boolean `is_processing`, inherited from the Strata-family server contract. If the installed Talus revision does not expose `/slots` with `is_processing`, set `probe: "gateway_only"` on the entry rather than claiming native occupancy.
- Example settings: 32K context default with 4K–256K adjustable presets. These are configurable settings, not a universal capacity or quality claim.
- Chat and raw completions use `/v1/chat/completions` and `/v1/completions` through the gateway's OpenAI-compatible proxy.

Model IDs must be unique across all engines; the suffixed `-talus` ID avoids duplicating Strata's, Kyojin's or Halogen Flash's Qwen3.8-Flash-Next ID. The client must still select the engine and load the model through the control API before inference.

## Host setup

1. Independently build/install Talus and obtain compatible GGUF weights. IGM does not install engines or download/convert weights.
2. Copy the Talus engine entry and its native matchers from `examples/config.example.json` into the host's **existing private configuration**. Preserve existing engines, credentials, LAN restrictions, recovery options and default selection. Do not replace a live host configuration with the generic example.
3. Replace `/opt/talus/bin/talus-server` and `/opt/models/qwen3.8-flash-next.gguf` with verified installed paths. Keep `base_url`/port consistent with the binary's `--host`/`--port`. Other supported families (27B, GLM, MiMo, Laguna) need their own reviewed model/profile entries if added.
4. Strix Halo requirements: the engine needs `/dev/kfd` and `/dev/dri` access plus a ROCm/HIP stack compatible with the host. IGM executes an argv array, not an interactive shell; add only verified host-specific runtime variables (for example an HSA override) to the model's `env`. Do not guess library paths in the portable example.
5. Retain the `talus-server` native matcher to detect competing Talus instances. Matchers detect conflicts, not ownership; a match never authorizes signalling an unmanaged process. If a wrapper renames the binary, adjust the matcher to its real executable name and verify detection. Do not match every `python` process.
6. Restart the actual gateway supervisor only during an authorized idle maintenance window, under external inference admission. Configuration is read at startup; there is no hot-reload API. Restart may load `default.engine`/`default.model`, not the previously unloaded state. Preserve a backup and a verified rollback path.

## Acceptance

Use the normal [controller procedure](orchestration.md), without printing keys:

1. Verify `/api/engines` lists the intended configured engines and the enabled Talus model. The catalog must not expose paths, argv or environment values.
2. Confirm no active requests or unmanaged native engines; select `talus` with `force:false` and read back selection.
3. Load `qwen3.8-flash-next-talus`, allowing the configured startup timeout. Verify `phase: ready`, exact model ID and native-slot occupancy. HTTP availability while `/health` is not yet 200 is not readiness.
4. Generate non-streamed chat, SSE chat and raw completions through IGM with the inference credential. Validate the actual response and stream termination, not merely HTTP 200. Exercise required tool-call workflows separately.
5. While generation is active, verify competing inference/lifecycle requests are refused without forcing a stop. If native occupancy cannot be read, do not downgrade to `gateway_only` to hide the problem.
6. Unload or switch normally; verify the exact native PID/start-time identity exited and no native processes survived. Restore the pre-test selection/model state when required.

Repository tests cover configured-engine discovery, sanitized model catalog, configuration-based selection, native process conflict detection and identity-verified fixture exit. Fixture tests do **not** certify actual GPU execution. Private real-engine acceptance evidence, runtime library paths and host deployment configuration must remain outside Git.
