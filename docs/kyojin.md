# Kyojin integration

IGM treats Kyojin as an administrator-configured native process, using the existing lifecycle, authenticated catalog and transparent inference proxy. The example now offers `llama`, `strata`, `kyojin`, `dwarfstar` and `gufo`; the dashboard renders that catalog dynamically. The gateway runtime needs no engine-specific Python or JavaScript adapter.

## Supported example

The initial entry runs Kyojin's `tools/qwen/serve.py` with a Qwen3.8-Flash-Next **EXL3 pack**, not the GGUF used by llama.cpp/Strata:

- Engine ID: `kyojin`; model ID: `qwen3.8-flash-next-kyojin`.
- Loopback backend: `http://127.0.0.1:8082`.
- Readiness: `/health` returns 503 during loading and 200 after warm-up.
- Occupancy: `probe: slots`, using `/slots` and boolean `is_processing`.
- Example settings: 32K context, int8 KV cache, MTP policy with four draft tokens, one session, vision disabled. These are configurable settings, not a universal capacity or quality claim.
- Chat and raw completions use `/v1/chat/completions` and `/v1/completions`. Kyojin's Qwen server does not provide `/v1/responses` or `/v1/messages`; IGM does not translate those APIs.

Model IDs must be unique across all engines. Use a suffixed Kyojin ID instead of duplicating Strata's ID. Keep `--model-id` identical to the gateway ID, or explicitly set `upstream_model` to the ID accepted by Kyojin. The client must still select the engine and load the model through the control API before inference.

## Host setup

1. Independently install/build Kyojin, its Python dependencies and the EXL3 pack. IGM does not install engines or download/convert weights.
2. Copy the Kyojin engine entry and its native matchers from `examples/config.example.json` into the host's **existing private configuration**. Preserve existing engines, credentials, LAN restrictions, recovery options and default selection. Do not replace a live host configuration with the generic example.
3. Replace `/opt/kyojin` and `/opt/models/qwen3.8-flash-next-exl3` with verified installed paths. Set `cwd`, `PYTHONPATH` and `EXL3_ROOT` to the Kyojin checkout. Ensure `--slot-save-path` exists or can be created by the engine-owning account; the example `/var/lib/...` directory needs administrator provisioning. Point `--chat-template` at the pack's template if that is the required model behavior; otherwise Kyojin uses its bundled Qwen template.
4. Verify the installed Kyojin runtime environment. In particular, on Strix Halo the selected HSA library and compiler SDK must be compatible with the installed Torch/ROCm stack. Consult the actual checkout's `tools/strix_halo/env.sh` and startup/compiler checks. If that installation requires `LD_PRELOAD`, `EXL3_ROCM_SDK` or other runtime variables, add their **verified host-specific values** to this model's `env`. The portable example deliberately does not guess an HSA library path or hardcode a Python site-packages version. IGM executes an argv array, not an interactive shell; shell activation/exports from another terminal do not configure a system service.
5. Retain the `tools/qwen/serve.py`, `tools/glm/serve.py` and `tools/mimo/serve.py` native matchers to detect competing Kyojin servers. Matchers detect conflicts, not ownership. Only Qwen is registered as a loadable model in this example; GLM and MiMo need their own reviewed argv/pack entries if added. Do not match every `python` process. If using renamed scripts or wrappers, adjust the matchers to their real native argv and verify detection.
6. Restart the actual gateway supervisor only during an authorized idle maintenance window, under external inference admission. Configuration is read at startup; there is no hot-reload API. Restart may load `default.engine`/`default.model`, not the previously unloaded state. Preserve a backup and a verified rollback path. A root-owned installation requires administrator access.

## Acceptance

Use the normal [controller procedure](orchestration.md), without printing keys:

1. Verify `/api/engines` lists exactly the intended five engines and the enabled Kyojin model. The catalog must not expose paths, argv or environment values.
2. Confirm no active requests or unmanaged native engines; select `kyojin` with `force:false` and read back selection.
3. Load `qwen3.8-flash-next-kyojin`, allowing the configured startup timeout for initial kernel tuning. Verify `phase: ready`, exact model ID and native-slot occupancy. HTTP availability while `/health` is 503 is not readiness.
4. Generate non-streamed chat, SSE chat and raw completions through IGM with the inference credential. Validate the actual response and stream termination, not merely HTTP 200. Exercise required tool-call workflows separately.
5. While generation is active, verify competing inference/lifecycle requests are refused without forcing a stop. If native occupancy cannot be read, do not downgrade to `gateway_only` to hide the problem.
6. Unload or switch normally; verify the exact native PID/start-time identity exited and no native processes survived. Restore the pre-test selection/model state when required.

Repository tests cover five-engine discovery, sanitized model catalog, configuration-based Kyojin selection, native process conflict detection for all three server families, native-slot lifecycle, fixture SSE forwarding and identity-verified exit. Fixture tests do **not** certify GPU execution. Private real-engine acceptance evidence, runtime library paths and host deployment configuration must remain outside Git. Boot-before-login, a second-device connection and full coding-client endurance are separate acceptance gates.
