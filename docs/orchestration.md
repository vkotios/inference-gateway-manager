# Agent and controller integration

IGM provides lifecycle and inference APIs, not a model-selection policy, task scheduler, orchestration plugin or distributed lease. An HTTP-capable agent can manage it independently of the provider running that agent. Loading a skill or setting an environment variable alone does not implement this integration.

The optional [remote stdio MCP adapter](features.md#mcp-on-another-machine) supplies discovery, explicit lifecycle control and bounded prompt delegation. It runs on the harness machine and does not execute returned tools or modify workspace files.

## Connections and authority

Use `IGM_BASE_URL=http://GATEWAY_HOST:9000` as a gateway-root convention. Supply keys through a private secret store/process environment; [client setup](clients.md) covers Windows and Unix. Never print credentials or include them in prompts, URLs, Git, request logs or error reports.

- Controller: `/api/...`, `Authorization: Bearer <IGM_CONTROL_KEY>`.
- Workers: `/v1/...`, `Authorization: Bearer <IGM_INFERENCE_KEY>`.
- Root `/` is the dashboard. Bare `/v1` is not a web interface.

Control authority includes force interruption and optionally administrator-enabled recovery operations. Restrict it to a trusted controller rather than distributing it to every worker. The inference key cannot access control routes. The control key can infer, but worker clients should not use it.

## Discovery and lifecycle contract

| Method | Route | Body / result |
|---|---|---|
| GET | `/api/engines` | `engines[]` with engine `id`, `label`, `models[]` containing `id`, `context`, `enabled`, `context_limits`, `reasoning_modes`, `reasoning_change` |
| GET | `/api/status` | `selected_engine`, `loaded_model`, `active_settings`, `selection_id`, `phase`, `busy`, `native_busy`, `occupancy_source`, `active_requests`, `requests`, `error`, process identities |
| GET | `/api/telemetry` | Host measurements; unavailable counters remain null |
| POST | `/api/engine` | `{"engine":"ENGINE_ID","force":false}`; select only, stop previous engine if switching |
| POST | `/api/model` | `{"model":"MODEL_ID","force":false}`; optional `engine`, `context`, `reasoning`; validate before stopping, context changes reload |
| POST | `/api/unload` | `{"force":false}`; stop model/backend, retain selected engine |
| GET | `/v1/models` | Enabled models with `owned_by`, `context_length`, loaded/unloaded `status` |
| POST | `/v1/chat/completions` | OpenAI-compatible body; `model` must equal loaded model; SSE supported |

`ENGINE_ID` and `MODEL_ID` are placeholders discovered from the catalog, not literal values to submit. Other inference transports are backend-dependent. Add `X-Client-Name` to inference requests for useful activity labels; never put secrets in that label.

Compare the requested context/reasoning with `active_settings` before reusing a ready model. Include its `selection_id` as `X-IGM-Selection-ID` on inference to reject settings that changed before admission. This guard does not reserve the backend or replace orchestration ownership. See [settings and client budgets](features.md#context-and-reasoning) for capability declarations and explicit client reasoning precedence.

## Recommended controller procedure

1. **Acquire your orchestration-level exclusive ownership.** Centralize the lifecycle controller or use an external coordination mechanism spanning the work interval, not just the switch request. IGM does not issue a durable lease. Dashboard operators and other controllers can still race.
2. **Discover the catalog and read status.** Select an enabled model under its actual engine. Treat catalog context as a configuration value, not proof of transport/tool compatibility.
3. **Reuse when ready.** If the requested engine/model already match and `phase == "ready"`, no lifecycle write is necessary. Workers may still receive a busy response; there is no queue.
4. **Before a change, inspect occupancy.** Active requests or `busy == true` mean wait/recheck. For native-slot backends, unknown `native_busy` is not idle. If the gateway is unloaded, unknown native-slot state can be normal; a `force:false` load remains subject to the gateway's unmanaged-process/admission checks. Do not invent an idle result to get past a refusal.
5. **Select the engine with `force:false`.** Verify the returned/read-back selection. Selection alone does not load a model. Selecting the already-selected engine is normally a no-op, not an unload.
6. **Load the model with `force:false`.** `/api/model` can remain pending for the model's startup duration; use a client timeout appropriate to the configured host startup timeout and resource conditions. There is no operation-ID polling API. On connection loss or timeout, re-read status before deciding whether to retry: do not assume the load completed or failed.
7. **Verify readiness.** Read `/api/status`: exact `selected_engine` and `loaded_model`, `phase == "ready"`, no startup error. Inspect occupancy immediately before dispatch; this snapshot does not reserve the inference slot. Confirm the desired model is marked loaded in `/v1/models`.
8. **Dispatch inference using the worker credential.** Keep the external ownership until intended work is finished. IGM admits one request at a time. All agents share the same loaded backend.
9. **Verify completion and release ownership.** Do not unload automatically after each request. Leaving the model warm is normally preferable. If unloading is required, use `force:false` and read back unloaded state (`loaded_model == null`, no active requests, selected engine retained).

### Normal switch example (PowerShell)

With `IGM_BASE_URL` and `IGM_CONTROL_KEY` supplied privately, discover valid IDs first:

```powershell
$base = $env:IGM_BASE_URL.TrimEnd('/')
$headers = @{ Authorization = "Bearer $env:IGM_CONTROL_KEY" }
Invoke-RestMethod -Uri "$base/api/engines" -Headers $headers
Invoke-RestMethod -Uri "$base/api/status" -Headers $headers
# Replace these placeholders only after inspecting discovery/status.
$engine = 'ENGINE_ID'
$model = 'MODEL_ID'
Invoke-RestMethod -Method Post -Uri "$base/api/engine" -Headers $headers `
  -ContentType 'application/json' -Body (@{engine=$engine; force=$false} | ConvertTo-Json)
# Choose a timeout based on host startup configuration; 900 is an example.
Invoke-RestMethod -Method Post -Uri "$base/api/model" -Headers $headers `
  -ContentType 'application/json' -TimeoutSec 900 `
  -Body (@{model=$model; force=$false} | ConvertTo-Json)
Invoke-RestMethod -Uri "$base/api/status" -Headers $headers
```

This is an operator walkthrough, not an implementation of locking, retry policy or scheduling. Do not run it blindly alongside other clients. Windows examples are documented syntax; automated repository checks exercise the API contract on fixture backends, not Windows itself.

## Conflicts, interruption and failure

- **401:** authentication failure. **403:** credential role, network, Host or Origin rejection. Do not weaken security to make a request pass.
- **409:** admission/lifecycle conflict or invalid current selection/model. Re-read status and coordinate. Do not retry continuously and do not promote to force automatically.
- **502:** upstream request/connection failure. **503:** unknown inference occupancy, unavailable backend or startup/shutdown failure. Preserve diagnostics, inspect status and ask an operator when reconciliation is needed.
- Unmanaged native processes block loading. The gateway detects conflicts without claiming ownership; it does not grant an agent permission to kill unrelated processes.
- **`force:true` requires explicit authority to interrupt the affected work.** It can cancel active inference or loading and stop the engine. Force does not grant ownership of unmanaged processes.
- If selection succeeds but loading fails, do not claim the prior model was restored. Inspect the actual state.
- A cancelled/lost client request can produce partial work. Neither lifecycle nor inference calls have a documented idempotency-key guarantee.
- DwarfStar's `gateway_only` occupancy measures proxy activity, not verified native-slot idle. Route all inference through IGM; direct loopback callers bypass that measurement.
- Recovery APIs/CLI are separate opt-in administrative features. A skill must not invoke gateway restart or host reboot as routine model switching; see [recovery](recovery.md).

## Agent skill

Load [the portable skill](../skills/inference-gateway-manager/SKILL.md) through your harness's documented mechanism. It supplies this operating discipline; it does not install tools, supply secrets, make scheduling decisions or enforce permissions. No automatic skill discovery across all harnesses is claimed.

## Acceptance checks

Separate each gate: authenticated discovery; correct-role rejection; normal switch and exact readiness readback; generation with the inference key; streaming/tool calls for each selected backend; busy refusal; explicitly approved force interruption; boot recovery; second-machine reachability. Fixture tests do not certify GPU/container deployment or full coding-client endurance.
