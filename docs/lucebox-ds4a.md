# Lucebox and ds4a integration

The public example offers seven independent engine selections: `llama`, `strata`, `kyojin`, `dwarfstar`, `gufo`, `lucebox` and `ds4a`. The existing **ds4 / DwarfStar entry retains its `dwarfstar` engine ID**, model ID and launch profile unchanged. `ds4a` is the gateway ID for [julianmb/ds4fa](https://github.com/julianmb/ds4fa), a separate ds4 fork; it does not replace ds4. [Lucebox](https://github.com/Luce-Org/lucebox) is another independent runtime.

The dashboard, HTTP catalog and MCP engine discovery use the administrator configuration. Selecting an engine does not start it. Registration adds a model without loading it. Only an explicit model load starts the configured process; inference never loads or resizes an engine automatically. The new example models are disabled and no startup default is added.

## Portable profiles

| Engine / profile | Launch contract | Backend | Readiness | Occupancy |
| --- | --- | --- | --- | --- |
| `dwarfstar` / `gguf` | Existing `/opt/ds4/ds4-server -m … --ctx …` | `127.0.0.1:8000` | `/v1/models` | `gateway_only` |
| `lucebox` / `gguf` | `/opt/lucebox/server/build/luce_server TARGET --max-ctx … --model-name MODEL --max-concurrency 1` | `127.0.0.1:8084` | `/health` | `gateway_only` |
| `ds4a` / `dspark-gguf` | `/opt/ds4a/ds4-server -m TARGET --rocm --ctx … --mtp DRAFT --dspark` | `127.0.0.1:8085` | `/v1/models` | `gateway_only` |

Every launch explicitly binds `--host 127.0.0.1` and its configured port. Lucebox takes a positional target file; ds4a uses `-m`. Lucebox's profile has no draft argument and overrides `LUCE_DS4_SPEC=0` to prevent inherited DeepSeek DSpark activation. ds4a's required `draft` artifact is bound to `--mtp` and must be a GGUF file inside an approved model root. Use a **ds4fa-native DSpark support GGUF** compatible with the target; a Lucebox draft with a similar name is not evidence of compatibility. Inspection checks confinement, GGUF headers and declared target architecture (`deepseek4`), not tensor compatibility, output quality or memory fit.

Both new entries offer 4K, 8K, 16K, 32K, 64K and 128K presets (4,096–131,072 tokens), with 32K as the example default. These are administrator-declared capacities, not measured hardware limits. Higher context requires an explicit load/reload and loses the previous process cache. `gateway_only` provides no native context readback; status must not be interpreted as confirming the engine's actual allocation.

Gateway model IDs are globally unique. Lucebox binds `--model-name` to the registered gateway ID. ds4a uses `upstream_model: deepseek-v4-flash`, so the proxy sends the native model name even when the gateway model is called `example-ds4a` or another unique registered ID. This profile targets Flash; other model families need their own reviewed profiles and upstream mapping.

## Administrator setup

1. Independently install/build each desired runtime and obtain compatible weights. IGM does not download, convert or install them. The ds4a profile explicitly selects ROCm; review that flag when adapting a build for another backend.
2. Copy only the desired engine entries and process matchers from [the example](../examples/config.example.json) into the private host configuration. Preserve existing credentials, LAN settings, model roots, recovery configuration and defaults. Keeping ds4 available in the public example does not require deploying it on every host.
3. Replace `/opt/lucebox`, `/opt/ds4a` and `/opt/models` with verified administrator-owned paths. Update the executable path, model and companion paths, and `base_url`/port together. Use the fixed argv arrays directly; the example does not invoke upstream helper scripts that change clocks, memory limits or download files. Add only verified runtime environment variables to the relevant entry; check inherited service variables as well.
4. Configure approved model roots, inspect/register through the dashboard, then load explicitly. Alternatively edit the disabled static examples with real paths and enable them. Merely enabling an entry does not launch it without a load or a configured startup default.
5. Use `luce_server` and `ds4-server` native matchers. Both ds4 and ds4fa normally build a binary named `ds4-server`, so that matcher deliberately detects either installation. Optional renamed `ds4a-server` and `ds4fa-server` binaries are also covered. No broad Python, shell or container-runtime matcher is added. If a wrapper renames the native executable, configure and verify its actual name. Matching a process detects a conflict; it never grants permission to kill an unmanaged process. Container wrappers must forward `IGM_ENGINE_RUN_ID` as described in the [README](../README.md).
6. Apply configuration through the actual supervisor only during an authorized idle maintenance window. Source example changes do not update installed configurations or reload a live process. Startup defaults may load a model on restart.

## Readiness, occupancy and acceptance

The launch/readiness contracts were checked against Lucebox revision [`9a2c0b6`](https://github.com/Luce-Org/lucebox/tree/9a2c0b60710839889911b410267204fb943ddf5b) (`server/src/server/server_main.cpp`, `http_server.cpp`) and ds4fa revision [`797a35f`](https://github.com/julianmb/ds4fa/tree/797a35fe7556891435eb0a6a93cd8796554e5cc3) (`src/ds4_server.c`, `src/ds4_help.c`). Verify these contracts against the installed revision. The selected readiness routes become reachable after native model initialization; they do not expose native slot occupancy to IGM. The new entries retain `probe: gateway_only` rather than claiming `/slots` support.

Route **all inference through IGM**. Direct loopback callers bypass tracked occupancy. `native_busy: false` in this mode means no tracked gateway request, not verified native idle. Do not expose native ports on the LAN. An unmanaged engine blocks selection/loading until its owner reconciles it externally.

Before trusting a deployed engine, select it with `force:false`, confirm selection without a process launch, load the intended model/context and inspect `/api/status`. Exercise non-streamed and streamed chat through the gateway, verify model mapping, output and stream termination, then verify normal switching/unload exits the native PID identity. Test busy rejection and required tool-call/API workflows separately. IGM does not translate unsupported APIs. Validate context fit and quality on the intended hardware and exact artifacts; presets are not GPU acceptance results.

Repository tests use temporary HTTP backends and synthetic process inventories. They cover catalog sanitization, context binding, DSpark artifact confinement, conflict detection, explicit loading, SSE forwarding and verified fixture exit. They do not certify GPU performance or native model correctness, and make no changes to installed services, host weights or private gateway configuration.
