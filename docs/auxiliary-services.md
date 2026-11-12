# CPU transcription and embeddings

The gateway supports independent, explicitly controlled CPU services. Install the optional `auxiliary` package extra in the gateway's own virtual environment and install the `ffmpeg` and `prlimit` executables. Install a CPU-only PyTorch build appropriate for the host. No model is downloaded or loaded by an inference request, and neither service auto-loads at startup.

Configure an `auxiliary` object in the gateway's administrator-owned JSON configuration. The following is an example fragment, not a complete gateway configuration. Replace every path and budget with values measured for your host, model, runtime, longest permitted request, and chat workload. Values shown are illustrative only, not validated OOM-safe limits. The process account must be able to read the model directories and write its state directory. The downloaded checkpoints in this workspace are under `.local/aux-models` and need suitable permissions if the production gateway runs as another user.

```json
"auxiliary": {
  "minimum_free_bytes": 8589934592,
  "chat_growth_bytes": 17179869184,
  "transcription": {"models": {
    "whisper-large-v3-turbo": {"path": "/srv/models/faster-whisper-large-v3-turbo", "load_bytes": 6442450944, "request_bytes": 4294967296, "threads": 4}
  }},
  "embeddings": {"models": {
    "all-MiniLM-L6-v2": {"path": "/srv/models/all-MiniLM-L6-v2", "load_bytes": 2147483648, "request_bytes": 1073741824, "threads": 2},
    "bge-small-en-v1.5": {"path": "/srv/models/bge-small-en-v1.5", "load_bytes": 2147483648, "request_bytes": 1073741824, "threads": 2},
    "Qwen3-Embedding-0.6B": {"path": "/srv/models/Qwen3-Embedding-0.6B", "load_bytes": 6442450944, "request_bytes": 4294967296, "threads": 2}
  }}
}
```

The control API exposes `GET /api/services` and `POST /api/services/{transcription|embeddings}/{enable|disable|load|unload}`. All POSTs take JSON; use `{}` except for load, which takes `{"model":"MODEL_ID"}`. The portal provides the same controls. Enable, then select and load. Disable rejects while work is active; unloading is explicit and also rejects while active. A worker processes one request at a time, but the two workers can run concurrently. Inference credentials can call `POST /v1/audio/transcriptions` and `POST /v1/embeddings`, but cannot control services.

The transcription endpoint accepts multipart `file`, `model`, optional `language`, `response_format` (`json` or `verbose_json`), and `timestamp_granularities[]` (`word`, `segment`). The verbose response includes word and segment timestamps and duration. It is ASR only, not speaker diarization. The implementation caps compressed uploads at 32 MiB and decoded audio at two hours; it returns an error instead of truncating. Audio is decoded in a separate process with a 2 GiB address-space limit and a corresponding memory reservation, to a private temporary file in the gateway state directory that is removed on completion. The embeddings endpoint accepts a string or up to 64 strings, with `float` or `base64` encoding. It refuses inputs that exceed the model token limit. It returns normalized embeddings and token usage. Models differ in vector dimensions and vector spaces: rebuild indexes when changing models, revisions, normalization, or query/document instruction policy. Qwen3 supports instruction-aware retrieval, but this generic endpoint does not add query instructions automatically.

Available physical and cgroup memory (excluding swap), outstanding reservations, model load/request budgets, the minimum reserve, and configured chat growth budget are checked before work. Each worker also receives a Linux address-space limit equal to the smaller of its load and request budgets; setting this too low makes the worker fail safely, and virtual address space is not the same as resident RAM. Unknown readings or unknown chat growth budget refuse admission. Chat model loads are refused while an auxiliary model is loaded; unload the auxiliary models explicitly before changing the chat selection. Existing chat inference may run alongside auxiliary requests if the configured headroom permits. A worker crash, an unrelated process or future chat allocation can exceed estimates: these checks reduce risk and do not guarantee avoidance of host OOM. Use OS memory limits where available and validate budgets under controlled representative workloads. Do not use aggressive test loads while the host is near its memory limit.

The Plaud OpenAI-compatible ASR and embedding settings should use the gateway URL ending in `/v1`, the inference key, and the exact configured model IDs. Word timestamps are returned for Plaud; its diarization remains a separate step. Validate complete indexing and retrieval against the deployed Plaud instance before relying on it. The downloaded Parakeet, Canary and Qwen ASR models are research candidates; they are not supported by the current Whisper worker.
