# Talus integration

The example targets the [Talus Rust server/native CLI contract](https://github.com/nsbradley88/talus-public/tree/c3805ee). It uses a dense Qwen3.8 27B GGUF as a minimal example; Flash-Next requires its own pack, native weights and tokenizer configuration and cannot be represented by this dense profile.

## Launch contract

- Engine `talus`, model `qwen3.8-27b-talus`; backend `http://127.0.0.1:8088`.
- `/opt/talus/bin/talus-server --config {config_path} --host 127.0.0.1 --port 8088 --ctx-size {context}`.
- IGM writes an administrator-owned `config_template` naming the real `/opt/talus/bin/talus-cli` as `exe`. The template passes the GGUF with `--model`, native context with `--ctx` and `--no-mtp` for a baseline without additional draft artifacts. The same GGUF supplies the tokenizer; the template defines model ID, stop token and sampling defaults.
- The outer `--ctx-size` binds server limits as well as engine context (32K default, 4K–256K example range). The server's CLI does **not** accept `--slots`; `/slots` is exposed without that flag.
- Readiness `/health`; occupancy `probe: slots`, using the server's boolean `is_processing` field. Verify this contract for the installed revision.

The real `exe` is essential: an unconfigured Talus server can use a mock backend. HTTP readiness alone is therefore insufficient evidence of real inference. Confirm the spawned `talus-cli` process and engine output. See upstream's [run-config example](https://github.com/nsbradley88/talus-public/blob/c3805ee/README.md).

## Setup and registration

Install the server, native CLI, kernel library and compatible ROCm/HIP runtime independently. Keep the kernel library beside the CLI or configure verified upstream runtime paths. Any required library directories belong in the administrator `config_template`/environment; IGM does not source an interactive shell. Ensure device access to `/dev/kfd` and `/dev/dri`.

Profile `gguf` binds the registered file to both native model arguments and tokenizer in the generated config. It validates GGUF structure and confinement, not compatibility or GPU fit. Other model families and accelerated drafters need separate reviewed profiles with their required companion assets.

Retain `talus-server` **and** `talus-cli` native matchers. They prevent unmanaged conflicts, not grant ownership. Copy the reviewed entries into the existing private gateway configuration, preserving security and other engines. Apply config through an idle, externally admitted supervisor restart with backup and rollback; source examples do not hot-reload an installed gateway.

## Acceptance

Use the [controller procedure](orchestration.md) with `force:false`: sanitized catalog, explicit selection/load and context readback, confirmed native CLI child, readiness/occupancy, non-streamed/SSE chat and raw completions, actual output/stream termination and busy rejection. Unload/switch normally and verify both exact process identities exited. Restore previous settings when required.

Repository tests check catalog/configuration, registration bindings, native matching and an HTTP lifecycle fixture. They do **not** certify native GPU execution or output quality. Keep private host paths and real-engine acceptance evidence outside Git.
