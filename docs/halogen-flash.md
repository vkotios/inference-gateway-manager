# Halogen Flash integration

IGM treats Halogen Flash as an administrator-configured **containerized** process, using the existing lifecycle, authenticated catalog and transparent inference proxy. The example offers `llama`, `strata`, `kyojin`, `dwarfstar`, `gufo`, `lucebox`, `ds4a`, `halogen`, `halogen-flash`, `talus` and `strixite`; the dashboard renders that catalog dynamically. The gateway runtime needs no engine-specific Python or JavaScript adapter.

## Supported example

The entry runs [peonist-ai/halogen-flash-server](https://github.com/peonist-ai/halogen-flash-server) with Podman and a Qwen3.8-Flash-Next **`.hgn` pack**:

- Engine ID: `halogen-flash`; model ID: `qwen3.8-flash-next-halogen-flash`.
- Launch: `podman run --rm` of `ghcr.io/peonist-ai/halogen-flash-server`, with `/dev/kfd` and `/dev/dri` passed through, the model root mounted read-only at the same absolute path, and loopback-only port publishing (`127.0.0.1:8087:8087`).
- Loopback backend: `http://127.0.0.1:8087`.
- Readiness: `/health` returns success only after the `.hgn` pack is loaded and warmed up.
- Occupancy: `probe: slots`, using `/slots` and boolean `is_processing`.
- Example settings: 32K context default with the standard 4K–128K adjustable presets. These are configurable settings, not a universal capacity or quality claim.
- Chat and raw completions use `/v1/chat/completions` and `/v1/completions` through the gateway's OpenAI-compatible proxy.

Model IDs must be unique across all engines; use the suffixed `halogen-flash` ID instead of duplicating Strata's, Kyojin's or Talus's Qwen3.8-Flash-Next ID. The client must still select the engine and load the model through the control API before inference.

## Host setup

1. Independently install Podman (or Docker with the equivalent flags), pull/build the Halogen Flash image, and obtain the `.hgn` pack. IGM does not install engines, pull images or download/convert weights. Halogen and Halogen Flash are separate engines: only one runs at a time, and switching between them stops the previous container first.
2. Copy the Halogen Flash engine entry and its native matchers from `examples/config.example.json` into the host's **existing private configuration**. Preserve existing engines, credentials, LAN restrictions, recovery options and default selection. Do not replace a live host configuration with the generic example.
3. Replace `/opt/models/qwen3.8-flash-next.hgn` and the volume mount with verified host paths. Keep the model root mounted read-only and at the same absolute path inside the container so the `{path}` registration binding stays valid. Adjust the image reference to the reviewed tag and keep its port distinct from Halogen's.
4. Strix Halo requirements: the container needs `/dev/kfd` (KFD) and `/dev/dri` (DRM render node), plus the `render` group. Verify with the actual engine documentation that these device passes are sufficient for the installed ROCm/HIP stack; add only verified device or environment entries to the model's `env` or argv.
5. Container lifecycle: IGM tracks the `podman run` process and its children. The example forwards the per-launch ownership tag into the container with `-e IGM_ENGINE_RUN_ID={launch_id}`; keep that argument so a detached container child remains owned and is verified to exit on switch/unload. The `halogen-flash-server` native matcher detects competing instances running outside IGM. Matchers detect conflicts, not ownership; a match never authorizes signalling an unmanaged process. Do not add a broad `podman`/`docker` matcher. Clean shutdown uses SIGTERM to the tracked processes, then SIGKILL after the configured timeout; `--rm` removes the stopped container.
6. Registration profile `hgn` accepts a single `.hgn` file inside an approved model root (`format: native` with `extensions: [".hgn"]`). Inspection checks confinement and the file suffix, not tensor compatibility, memory fit or output quality.
7. Restart the actual gateway supervisor only during an authorized idle maintenance window, under external inference admission. Configuration is read at startup; there is no hot-reload API. Restart may load `default.engine`/`default.model`, not the previously unloaded state. Preserve a backup and a verified rollback path.

## Acceptance

Use the normal [controller procedure](orchestration.md), without printing keys:

1. Verify `/api/engines` lists the intended configured engines and the enabled Halogen Flash model. The catalog must not expose paths, argv or environment values.
2. Confirm no active requests or unmanaged native engines (including hand-started Halogen Flash containers); select `halogen-flash` with `force:false` and read back selection.
3. Load `qwen3.8-flash-next-halogen-flash`, allowing the configured startup timeout for container start and pack loading. Verify `phase: ready`, exact model ID and native-slot occupancy. HTTP availability while `/health` is not yet 200 is not readiness.
4. Generate non-streamed chat, SSE chat and raw completions through IGM with the inference credential. Validate the actual response and stream termination, not merely HTTP 200. Exercise required tool-call workflows separately.
5. While generation is active, verify competing inference/lifecycle requests are refused without forcing a stop. If native occupancy cannot be read, do not downgrade to `gateway_only` to hide the problem.
6. Unload or switch normally; verify the exact native PID/start-time identities (including the container's engine process) exited and no native processes or leftover containers survived. Restore the pre-test selection/model state when required.

Repository tests cover configured-engine discovery, sanitized model catalog, configuration-based selection and native process conflict detection. Fixture tests do **not** certify actual GPU execution or container teardown. Private real-engine acceptance evidence, image digests and host deployment configuration must remain outside Git.
