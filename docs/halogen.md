# Halogen integration

Halogen is an administrator-owned Podman launch profile. IGM does not install an engine, pull an image, download weights or convert them. The example is based on [Halogen 0.1.4](https://github.com/peonist-ai/halogen-server/tree/5a0f952) and uses `ghcr.io/peonist-ai/halogen:0.1.4` (the image is named `halogen`, not `halogen-server`). Review and pin an image digest for deployment.

## Launch contract

- Engine `halogen`, model `qwen3.8-27b-halogen`; backend `http://127.0.0.1:8086`.
- Run the image's `all` mode. There is no `serve --model ... --ctx ...` interface. Configure the image through Podman's `-e NAME=value` arguments.
- `HALOGEN_CHECKPOINT=/opt/models/halogen-qwen38-27b/qwen3.8-27b-p1w4d-d2.hgn` and `HALOGEN_TOKENIZER=/opt/models/halogen-qwen38-27b/tokenizer`. The tokenizer must be a flat directory containing `tokenizer.json` and `chat_template.jinja`, not dangling HF-cache symlinks.
- `HALOGEN_SLOT_CTX={context}` binds the selected native context (32K default, 4K–128K example presets). `HALOGEN_CTX` is not this dense engine's per-slot setting.
- Publish only `127.0.0.1:8086:8731`. The API listens on container port 8731; its internal unauthenticated engine port 8730 stays unpublished.
- Mount the approved model root read-only at the same absolute path inside the container. The installer rewrites both bind-mount paths and `-e NAME=/opt/models/...` arguments to the selected model root.
- Device/runtime flags follow upstream's rootless Podman recipe: `/dev/kfd`, `/dev/dri`, `--group-add keep-groups`, `--ipc=host`, `--ulimit memlock=-1:-1` and `seccomp=unconfined`. Fedora SELinux needs `label=disable` for the shared volume. `keep-groups` is Podman-specific; a Docker deployment needs its own reviewed group/device setup.

Readiness uses `/health`. Occupancy is **`gateway_only`**: this profile does not assert a verified llama.cpp-compatible `/slots` response with boolean `is_processing`. Route all inference through IGM; direct backend requests bypass gateway occupancy tracking. Never label this as verified native idle.

## Registration and lifecycle

Profile `hgn` accepts `.hgn` checkpoints and requires a named `tokenizer` directory artifact containing `tokenizer.json` and `chat_template.jinja`. Artifact inspection checks confinement and required files, not checkpoint tensor compatibility or available GPU memory.

Keep `-e IGM_ENGINE_RUN_ID={launch_id}` in the Podman arguments. It propagates IGM's ownership tag into container descendants; it does not authorize signalling unmanaged engines. Native matchers cover the actual `halogen` executable and `/halogen/tools/serve_api.py` API process. A broad `podman` or `python` matcher is inappropriate. Container descendant visibility/ownership and complete teardown still require real-host acceptance.

Copy the reviewed entry and matchers into the existing private host config, preserving keys, LAN restrictions, recovery settings and other engines. Config is read at startup. Restart the actual supervisor only when idle under external inference admission, with a backup and rollback plan. A source-example edit does not change an installed gateway.

## Acceptance

Follow the [controller procedure](orchestration.md) using `force:false`. Verify sanitized `/api/engines` discovery, explicit engine/model selection and selected context readback, readiness, non-streamed chat, SSE chat, raw completions, and busy rejection. Verify actual responses and stream termination. On unload/switch, confirm the exact tracked PID/start-time identities and the container's engine/API processes exited and `--rm` removed the container. Restore the previous selection/settings when required.

Repository tests cover catalog, companion registration, context binding, native conflicts and generic lifecycle fixtures. They do **not** certify real GPU inference, upstream image contents or container teardown. Keep real-engine acceptance evidence and private host paths outside Git.
