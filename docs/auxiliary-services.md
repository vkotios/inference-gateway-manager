# CPU transcription and embeddings

The gateway supports independent, explicitly controlled CPU services. Install the optional `auxiliary` package extra in the gateway's own virtual environment and install the `ffmpeg` and `prlimit` executables. Install a CPU-only PyTorch build appropriate for the host. No model is downloaded or loaded by an inference request, and neither service auto-loads at startup.

Configure an `auxiliary` object in the gateway's administrator-owned JSON configuration. The following is an example fragment, not a complete gateway configuration. The process account must be able to read the model directories and write its state directory.

```json
"auxiliary": {
  "minimum_free_bytes": 4294967296,
  "host_baseline_bytes": 6442450944,
  "restore": "never",
  "restore_holdoff_seconds": 3600,
  "drain_seconds": {"embeddings": 30, "transcription": 600},
  "transcription": {"models": {
    "whisper-large-v3-turbo": {"path": "/srv/models/faster-whisper-large-v3-turbo", "threads": 4}
  }},
  "embeddings": {"models": {
    "all-MiniLM-L6-v2": {"path": "/srv/models/all-MiniLM-L6-v2", "threads": 2},
    "bge-small-en-v1.5": {"path": "/srv/models/bge-small-en-v1.5", "threads": 2}
  }}
}
```

Model memory is **measured, not configured**. `load_bytes` and `request_bytes` are optional first-load seeds in bytes; without them the first load uses a conservative probation budget (four times the model's size on disk, at least 1 GiB). After a load the gateway records the worker's resident size and raises the recorded peak as requests are served, so models of any size and any family are handled by the same rules. `minimum_free_bytes` is the host reserve, `host_baseline_bytes` the memory used by everything else on the host (operating system, desktop, other services); both are subtracted from total memory to give the budget. `chat_growth_bytes` (default 0) optionally reserves extra live headroom while a chat model is loaded.

The control API exposes `GET /api/services` and `POST /api/services/{transcription|embeddings}/{enable|disable|load|unload}`. All POSTs take JSON; use `{}` except for load, which takes `{"model":"MODEL_ID"}`. The portal provides the same controls. Enable, then select and load. Disable rejects while work is active; unloading is explicit and also rejects while active. A worker processes one request at a time, but the two workers can run concurrently. Inference credentials can call `POST /v1/audio/transcriptions` and `POST /v1/embeddings`, but cannot control services.

The transcription endpoint accepts multipart `file`, `model`, optional `language`, `response_format` (`json` or `verbose_json`), and `timestamp_granularities[]` (`word`, `segment`). The verbose response includes word and segment timestamps and duration. It is ASR only, not speaker diarization. The implementation caps compressed uploads at 32 MiB and decoded audio at two hours; it returns an error instead of truncating. Audio is decoded in a separate process with a 2 GiB address-space limit and a corresponding memory reservation, to a private temporary file in the gateway state directory that is removed on completion. The embeddings endpoint accepts a string or up to 64 strings, with `float` or `base64` encoding. It refuses inputs that exceed the model token limit. It returns normalized embeddings and token usage. Models differ in vector dimensions and vector spaces: rebuild indexes when changing models, revisions, normalization, or query/document instruction policy. Qwen3 supports instruction-aware retrieval, but this generic endpoint does not add query instructions automatically.

## Memory, chat priority and footprints

A **footprint** is the host memory a loaded model holds. The gateway keeps footprints for chat models and auxiliary models in `footprints.json` in its state directory. Whether a combination fits is computed only from these records: the sum of the loaded chat model and every loaded auxiliary model (each at its recorded peak, plus a request allowance) must not exceed total memory minus `minimum_free_bytes` and `host_baseline_bytes`. Total memory is capped by any visible cgroup limit; swap is excluded. A larger transcription model therefore automatically changes which chat models can run beside it, and the other way round.

Rules:

- **Chat has priority.** A chat load never waits on or is refused because of auxiliary services. If the target chat model fits with the loaded services nothing is unloaded. Otherwise the services that do not fit are unloaded, largest first. New requests to a service that is being unloaded get an immediate retryable `503` (`X-IGM-Code: auxiliary_unloaded_for_chat`, with `Retry-After`); requests already running may finish for up to `drain_seconds` (defaults: 30 s embeddings, 600 s transcription), then the worker is stopped. Services unloaded this way stay enabled and keep the operator's desired model.
- **A chat model with no recorded footprint cannot have services alongside it.** Loading it unloads every service, and loading a service while it is loaded is refused (`409`, `X-IGM-Code: model_uncalibrated`). Chat loads themselves are not blocked.
- **Records are valid only for the launch they were measured under.** Each record carries a fingerprint of the launch (engine, model, argument template, environment); changing, for example, KV-cache slots or the engine image makes the record stale and the model uncalibrated until it is recorded again. A record covers loads at the same or a smaller context, never a larger one.
- **Unknown or unreadable memory counters fail closed.** A damaged `footprints.json` is treated as empty.

Footprints are recorded automatically: when a chat model reaches ready the gateway measures the larger of the system-wide increase in used memory since just before launch and the engine's own total (proportional resident memory plus GPU memory attributed to its processes), excluding auxiliary workers. A high-water mark is kept while the model stays loaded and written after a settle period and whenever it grows meaningfully. `MemAvailable` alone is not used for the record because it counts reclaimable page cache as available, so a memory-mapped model can look almost free. Auxiliary workers are measured at load and after each request.

Control API (control credential, same CSRF rules as other controls):

- `GET /api/footprints`: records (with `stale` marks), the model currently being sampled, memory totals, and a compatibility list: each configured chat model at its maximum context with whether it is calibrated and whether it fits with the currently loaded services.
- `POST /api/footprints/record`: write the current high-water mark for the loaded chat model now.
- `POST /api/footprints/calibrate` with `{"engine": ..., "model": ...}`: reload that model at its maximum context, wait for it to settle and record it. The chat model currently loaded is unloaded; a guard stops the engine and aborts if available memory falls below `footprints.abort_below_available_bytes` (default 1 GiB). Use this for large models you do not want to discover the limits of in normal use.
- `POST /api/footprints/forget` with `{"kind": ..., "key": ...}`.

In the portal, **Calibrate at max context** sits next to **Load model** in the Engine lifecycle card (with the selected model's footprint and whether it fits with the loaded services), and the **Chat model footprints** table in the Auxiliary services card has a **Calibrate** button on every row and a **Record now** button. Calibrating asks for confirmation because it unloads the current chat model and the services. Optional `footprints` configuration: `interval_seconds` (default 5), `settle_seconds` (default 30), `abort_below_available_bytes`. Optional top-level `memory_footprints`: a list of administrator estimates, each `{"kind": "chat|transcription|embeddings", "key": "engine/model" or the auxiliary model ID, "steady_bytes": N, "peak_bytes": N, "context": N}`; estimates are always labelled as estimates and are replaced by a measurement for the same launch.

## Restore

Desired state (enabled and the model the operator last loaded) is saved in `auxiliary-state.json`. `auxiliary.restore` controls what the gateway does automatically. It can be changed without editing the config: the portal's **Auxiliary services** card has a **Restore** and a **Restore delay** setting, backed by `GET` and `POST /api/services/settings` (`{"restore": ..., "restore_holdoff_seconds": ...}`). A saved value overrides the config value, is kept across restarts, and takes effect immediately (the background loop starts or stops, no restart). The options are:

- `never` (default): nothing is restored. After a restart services are disabled; services unloaded for a chat model stay unloaded until loaded again.
- `after_clean_shutdown`: after a clean gateway stop, services the operator had loaded are loaded again at start. Services unloaded for a chat model are loaded again `restore_holdoff_seconds` (default 3600) after the memory is free, if they fit.
- `on_demand`: no automatic loading; the first request for an unloaded service that the operator had loaded starts it (the request gets a retryable `503` with `X-IGM-Code: auxiliary_restoring` meanwhile).

Automatic restore is guarded against crash loops: it is skipped, and shown in the portal, after an unclean shutdown, after an automatic restore that did not complete (a marker is written before and cleared after each attempt), and after three failed attempts within an hour. An explicit operator load clears the block. A worker that fails or whose request is cancelled is restarted by the same mechanism (when restore is not `never`). If a chat load fails after services were unloaded for it, they are restored only by this mechanism or by the operator.

An explicit unload or disable clears the desired state and is never undone automatically.

Memory checks are best effort: a worker crash, an unrelated process or later chat allocation can exceed any recorded number, and these checks do not guarantee avoidance of a host out-of-memory event. Each worker receives a Linux address-space limit (a safety cap, not an accounting figure; virtual address space is not resident RAM). Use OS memory limits where available and validate on your host. Do not use aggressive test loads while the host is near its memory limit.

The Plaud OpenAI-compatible ASR and embedding settings should use the gateway URL ending in `/v1`, the inference key, and the exact configured model IDs. Word timestamps are returned for Plaud; its diarization remains a separate step. Validate complete indexing and retrieval against the deployed Plaud instance before relying on it. The downloaded Parakeet, Canary and Qwen ASR models are research candidates; they are not supported by the current Whisper worker.
