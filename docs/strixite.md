# Strixite integration

IGM treats Strixite as an administrator-configured native process, using the existing lifecycle, authenticated catalog and transparent inference proxy. The example offers `llama`, `strata`, `kyojin`, `dwarfstar`, `gufo`, `lucebox`, `ds4a`, `halogen`, `halogen-flash`, `talus` and `strixite`; the dashboard renders that catalog dynamically. The gateway runtime needs no engine-specific Python or JavaScript adapter.

## Supported example

[Strixite](https://github.com/shawnshekari/strixite) is a from-scratch HIP engine for Qwen3.8-Flash-Next built for Strix Halo, serving **GGUF** files:

- Engine ID: `strixite`; model ID: `qwen3.8-flash-next-strixite`.
- Launch: native binary `/opt/strixite/bin/strixite` bound to `127.0.0.1:8089`.
- Loopback backend: `http://127.0.0.1:8089`.
- Readiness: `/v1/models` returns success once the model is served.
- Occupancy: `probe: "gateway_only"`. The from-scratch server does not expose the llama.cpp-style `/slots` endpoint with `is_processing`, so occupancy is measured at the gateway proxy, **not native slot telemetry**. Route all inference through IGM; direct loopback callers bypass that measurement and must not be used. Never label this mode as verified native idle.
- Example settings: 32K context default with the standard 4K–128K adjustable presets. These are configurable settings, not a universal capacity or quality claim.
- Chat and raw completions use `/v1/chat/completions` and `/v1/completions` through the gateway's OpenAI-compatible proxy.

Model IDs must be unique across all engines; the suffixed `-strixite` ID avoids duplicating Strata's, Kyojin's, Talus's or Halogen Flash's Qwen3.8-Flash-Next ID. The client must still select the engine and load the model through the control API before inference.

## Host setup

1. Independently build/install Strixite and obtain a compatible Qwen3.8-Flash-Next GGUF. IGM does not install engines or download/convert weights.
2. Copy the Strixite engine entry and its native matchers from `examples/config.example.json` into the host's **existing private configuration**. Preserve existing engines, credentials, LAN restrictions, recovery options and default selection. Do not replace a live host configuration with the generic example.
3. Replace `/opt/strixite/bin/strixite` and `/opt/models/qwen3.8-flash-next.gguf` with verified installed paths. Keep `base_url`/port consistent with the binary's `--host`/`--port`. Registration profile `gguf` validates GGUF headers and confinement inside approved model roots, not tensor compatibility, memory fit or output quality.
4. Strix Halo requirements: as a HIP engine, Strixite needs `/dev/kfd` and `/dev/dri` access and a compatible ROCm/HIP runtime (for example `HSA_OVERRIDE_GFX_VERSION` when the detected gfx target is unsupported). IGM executes an argv array, not an interactive shell; add only verified host-specific values to the model's `env`. Do not guess library paths in the portable example.
5. Retain the `strixite` native matcher to detect competing Strixite instances. Matchers detect conflicts, not ownership; a match never authorizes signalling an unmanaged process. If a wrapper renames the binary, adjust the matcher to its real executable name and verify detection.
6. Restart the actual gateway supervisor only during an authorized idle maintenance window, under external inference admission. Configuration is read at startup; there is no hot-reload API. Restart may load `default.engine`/`default.model`, not the previously unloaded state. Preserve a backup and a verified rollback path.

## Acceptance

Use the normal [controller procedure](orchestration.md), without printing keys:

1. Verify `/api/engines` lists the intended configured engines and the enabled Strixite model. The catalog must not expose paths, argv or environment values.
2. Confirm no active requests or unmanaged native engines; select `strixite` with `force:false` and read back selection.
3. Load `qwen3.8-flash-next-strixite`, allowing the configured startup timeout. Verify `phase: ready` and exact model ID. Occupancy source is `gateway_only`: processing means a tracked gateway request, not verified native idle.
4. Generate non-streamed chat, SSE chat and raw completions through IGM with the inference credential. Validate the actual response and stream termination, not merely HTTP 200. Exercise required tool-call workflows separately.
5. While generation is active, verify competing inference/lifecycle requests are refused without forcing a stop.
6. Unload or switch normally; verify the exact native PID/start-time identity exited and no native processes survived. Restore the pre-test selection/model state when required.

Repository tests cover configured-engine discovery, sanitized model catalog, configuration-based selection and native process conflict detection. Fixture tests do **not** certify actual GPU execution. Private real-engine acceptance evidence, runtime library paths and host deployment configuration must remain outside Git.
