# Halogen Flash integration

This administrator-owned Podman profile targets [Halogen Flash 0.19.1](https://github.com/peonist-ai/halogen-flash-server/tree/47ceb43), image `ghcr.io/peonist-ai/halogen-flash-server:0.19.1`. Review and pin an image digest before deployment. IGM does not install engines, pull images, download weights or perform conversion.

## Checkpoint and launch contract

Engine `halogen-flash` serves model `qwen3.8-flash-next-halogen-flash` at `http://127.0.0.1:8087`. The default upstream checkpoint layout is:

```text
halogen-qwen38-flash-next/
  qwen38-flash-next-v2.hgn
  qwen38-flash-next-ngram.hgn
  qwen38-flash-next-vision.hgn
  tokenizer/
    tokenizer.json
    chat_template.jinja
```

Download these selected files and the flat tokenizer from [the official checkpoint repository](https://huggingface.co/peonist-ai/halogen-qwen3.8-flash-next). Do not download the older w4b checkpoint and both overlay variants just to run v2: the supported default needs its lookup table, vision weights and tokenizer; v2 carries its draft head. The checkpoint and lookup table alone occupy about 110 GiB on disk. Review upstream's [precision and required assets](https://github.com/peonist-ai/halogen-flash-server/blob/47ceb43/docs/precision.md).

Run image mode `all`, with `HALOGEN_CHECKPOINT`, `HALOGEN_TOKENIZER`, `HALOGEN_MODEL_ID` and `HALOGEN_CTX={context}` passed as `-e NAME=value` arguments. There is no `serve --model ... --ctx ... --slots` command. The example binds 32K context with 4K–128K selectable presets. Publish only `127.0.0.1:8087:8731`, leaving the internal unauthenticated engine port unpublished.

Mount the approved model root read-only at the same absolute path. Keep `-e IGM_ENGINE_RUN_ID={launch_id}` so detached descendants retain their ownership identity. Upstream's rootless Podman device/group flags, IPC/memlock settings and the Fedora SELinux volume flag are shared with [Halogen](halogen.md). Docker needs a separately reviewed group configuration. Verify the installed ROCm/HIP stack and host memory settings against upstream requirements before loading.

Readiness is `/health`. Occupancy is **`gateway_only`**: upstream [documents `/slots`](https://github.com/peonist-ai/halogen-flash-server/blob/47ceb43/docs/api.md) as `{id, n_ctx}` entries, without the boolean `is_processing` required by IGM's native-slots probe. All inference must use IGM; direct backend callers bypass occupancy measurement. This mode is not verified native idle.

## Registration and deployment

Profile `hgn` accepts a `.hgn` checkpoint with the required sibling lookup table and vision checkpoint. Supply a named `tokenizer` directory artifact with `tokenizer.json` and `chat_template.jinja`. Inspection verifies confinement/required files; it does not validate tensors, GPU fit or quality. Alternate checkpoint layouts need separate reviewed profiles.

Retain native matchers for `flash_serve` and `/halogen/tools/serve_api.py`. Matching blocks conflicts; it never authorizes signalling unmanaged processes. Copy the reviewed profile into the existing private gateway configuration, preserving other engines and security settings. Perform an idle, externally admitted supervisor restart with backup/rollback; installed configs are not hot-reloaded.

## Acceptance

Follow the [controller procedure](orchestration.md) with `force:false`. Verify catalog sanitization, explicit selection/load and context readback, health, real non-streamed/SSE chat and raw completions, stream termination, busy rejection, normal unload/switch and exact descendant/container exit. Test required tool-call workflows separately. Restore previous settings when required.

Repository tests validate configuration, registration and conflict/lifecycle plumbing with fixtures. They do **not** certify actual GPU execution, container image contents or teardown. Keep private host paths, image digests and real-engine evidence outside Git.
