# Changelog

## Unreleased

- Transcription and embeddings now run alongside the chat model whenever recorded memory footprints fit, instead of being refused whenever a chat model is loaded. Fit is computed from recorded footprints only, so it is model agnostic.
- The chat model has priority: a chat load unloads only the services that do not fit (all of them when the chat model has no recorded footprint) and is never refused or blocked by them. Idempotent chat loads and reasoning/system-prompt changes are no longer refused while a service is loaded.
- Footprints are recorded automatically while models run, and can be recorded or calibrated at maximum context from the control API and portal; stale after launch changes.
- Auxiliary model budgets in configuration are now optional seeds; transcription reserves memory once instead of twice; loading a service no longer holds the lifecycle lock during worker start.
- Optional guarded restore of services (`auxiliary.restore`), with a hold-off, clean-shutdown requirement, in-progress marker and failure cap. The policy and delay can be changed from the portal or `/api/services/settings` and apply immediately.
- Portal: status tiles (inference activity, system memory, GPU, host/API) moved into an equal-size left rail beside the work cards; "Transcription and embeddings" is now "Auxiliary services", built like the engine card, with a visible footprint table and Calibrate buttons (also next to Load model).

## v1.0.0

- Interactive fresh-install onboarding for all seven engine selections, with explicit LAN settings, private credentials and optional prepared system-service setup.
- Opt-in engine version checks, isolated staging, compatibility checks and idle activation with explicitly authorized model-load and streaming inference verification.
- Retained executable, environment and configuration snapshots, verified rollback, offline configuration recovery and editable GitHub problem-report drafts.
- Lucebox and ds4a engine selections alongside llama.cpp, Strata, Kyojin, DwarfStar and Gufo; ds4 remains available as `dwarfstar`.
- Explicit engine/model lifecycle, separate control/inference credentials, remote client MCP, context/reasoning controls, maintenance leases and generation metrics.

Installation and rollback instructions are in the README and linked guides. Engines, GPU dependencies and weights are installed independently. Fixture and browser tests do not certify every engine release or target hardware; validate enrolled recipes and representative models on your deployment. Gateway package upgrades remain manual.
