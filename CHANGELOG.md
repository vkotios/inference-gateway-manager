# Changelog

## v2.0.0

This release adds optional CPU transcription and embeddings services that run beside the chat model, a model-agnostic memory ledger that decides what can coexist, and a reorganised portal.

### Added

- Optional CPU services: `POST /v1/audio/transcriptions` (faster-whisper, word and segment timestamps) and `POST /v1/embeddings` (sentence-transformers), each in its own isolated worker, controlled explicitly from the portal and `/api/services`. They never load a model or change the chat engine on their own. See [auxiliary services](docs/auxiliary-services.md).
- Memory footprints: every loaded chat model and auxiliary model has a recorded footprint, recorded automatically while it runs and calibratable at maximum context from the API and portal. Whether a combination fits is computed from those records only, so any model family and any size are handled by the same rules.
- Guarded restore of services (`auxiliary.restore`: `never`, `after_clean_shutdown` or `on_demand`) with a hold-off, an in-progress marker and a failure cap. The policy and delay can be changed from the portal or `/api/services/settings` and apply immediately.
- MCP: read-only `gateway_services` and `gateway_footprints` tools.
- Installer: optional, default-off step that installs CPU-only PyTorch and the `auxiliary` extra and checks for `ffmpeg` and `prlimit`.

### Changed

- The chat model has priority. A chat load unloads only the services that do not fit with it (all of them when its footprint is unrecorded) and is never refused or blocked by them. Idempotent chat loads and reasoning or system-prompt changes are no longer refused while a service is loaded.
- Loading a service no longer holds the lifecycle lock during worker start; transcription reserves memory once instead of twice.
- Auxiliary model `load_bytes` and `request_bytes` in the configuration are optional first-load seeds; measured footprints replace them.
- Portal: the status tiles (inference activity, system memory, GPU, host/API) sit in an equal-size left rail beside the work cards. "Transcription and embeddings" is now "Auxiliary services", built like the engine card, with a visible footprint table and Calibrate buttons (also next to Load model).
- The MCP adapter now exposes eight tools instead of six, and the installer asks one additional question.

### Security

- Transcription decoding is restricted to local files and common audio and video containers (ffmpeg `-protocol_whitelist file,pipe` and `-format_whitelist`). Crafted playlists, concat or SDP scripts can no longer make ffmpeg open network addresses or other files on the gateway's behalf. Uploads in a format outside the allowlist are refused with a 400.

### Upgrading from 1.0.0

No configuration change is required. The state directory gains `footprints.json` and `auxiliary-state.json`; older versions ignore them. Chat models without a recorded footprint unload the services when loaded; load each model once (or use Calibrate) to record it. Gateway package upgrades remain manual: see [installation](docs/installation.md#existing-installations-and-updates).

## v1.0.0

- Interactive fresh-install onboarding for all seven engine selections, with explicit LAN settings, private credentials and optional prepared system-service setup.
- Opt-in engine version checks, isolated staging, compatibility checks and idle activation with explicitly authorized model-load and streaming inference verification.
- Retained executable, environment and configuration snapshots, verified rollback, offline configuration recovery and editable GitHub problem-report drafts.
- Lucebox and ds4a engine selections alongside llama.cpp, Strata, Kyojin, DwarfStar and Gufo; ds4 remains available as `dwarfstar`.
- Explicit engine/model lifecycle, separate control/inference credentials, remote client MCP, context/reasoning controls, maintenance leases and generation metrics.

Installation and rollback instructions are in the README and linked guides. Engines, GPU dependencies and weights are installed independently. Fixture and browser tests do not certify every engine release or target hardware; validate enrolled recipes and representative models on your deployment. Gateway package upgrades remain manual.
