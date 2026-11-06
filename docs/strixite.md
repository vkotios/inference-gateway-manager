# Strixite integration

The example follows [Strixite's native server contract](https://github.com/shawnshekari/strixite/tree/0945ce8). The executable is **`strix_server`**, and it serves converted **`.strixw` weights**, not a GGUF passed to a binary named `strixite`.

## Launch contract

- Engine `strixite`, model `qwen3.8-flash-next-strixite`; backend `http://127.0.0.1:8089`.
- `/opt/strixite/bin/strix_server --weights .../weights.strixw --ngram .../ngram.table --tokenizer .../tokenizer.json --generation-config .../generation_config.json --vision off --host 127.0.0.1 --port 8089 --capacity {context}`.
- Context uses `--capacity` (32K default, 4K–128K example presets), not `--ctx`. `--model` is not this server's weight argument.
- The minimal example disables vision. To test vision, obtain the converted vision weights and use a separate reviewed profile with the appropriate required assets and `--vision on`.
- Readiness uses `/v1/models`. Occupancy is **`gateway_only`**, without a claimed native `is_processing` probe. Route every inference request through IGM; direct backend traffic bypasses occupancy tracking. Never describe this as verified native idle.

Obtain/convert the weights, n-gram table and tokenizer assets independently using the upstream [setup instructions](https://github.com/shawnshekari/strixite/blob/0945ce8/README.md). IGM neither downloads nor converts them. Configure verified ROCm/HIP runtime paths and device access; the gateway invokes an argv array, not an interactive shell.

## Registration and lifecycle

Profile `strixw` uses `format: native`, `extensions: [".strixw"]`. Supply named `ngram` (a `.table` file) and `tokenizer` (a directory containing `tokenizer.json` and `generation_config.json`) companion artifacts. Binding preserves these paths when a model is registered. Inspection checks confinement and required files, not tensor compatibility, memory fit or quality.

Retain the **`strix_server`** native matcher: the repository name `strixite` is not the running executable's name. Matching prevents unmanaged conflicts and does not grant ownership or permission to kill unmanaged work.

Copy the reviewed profile into the existing private configuration, preserving other engines and security settings. Restart the actual supervisor only when idle under external inference admission, with config backup and rollback. Installed configs are read at startup, not hot-reloaded.

## Acceptance

Follow the [controller procedure](orchestration.md) with `force:false`. Verify sanitized discovery, explicit engine/model selection, context readback, readiness, actual non-streamed/SSE chat, supported raw completion behavior and busy rejection. Confirm exact native PID/start-time exit after normal unload/switch and restore prior settings when required. Verify endpoint support against the pinned server; an OpenAI chat endpoint alone does not prove every completion route exists.

Repository tests validate catalog, artifact bindings, conflict detection and generic lifecycle plumbing. They do **not** certify actual GPU execution or upstream endpoint coverage. Keep private paths and real-engine acceptance evidence outside Git.
