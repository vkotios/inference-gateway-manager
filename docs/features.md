# Model controls, registration, browser sessions and remote MCP

IGM keeps one engine/model loaded and admits one inference request at a time. Workers
continue to use `/v1` with the inference key. Operators explicitly load settings through
the dashboard, control API or MCP. An inference request never implicitly loads a model.

## Context and reasoning

The model's context capacity, the client's conversation budget and the response-token
limit are different settings. A client configured for 65,536 tokens can use an engine
loaded at 262,144 tokens without resizing it. Shorter prompts consume less work but do
not promise a smaller engine allocation. To change engine capacity, explicitly reload:

```json
{"engine":"llama","model":"my-model","context":65536,"reasoning":"off","force":false}
```

Send this to `POST /api/model` with the control key. `engine`, `context` and `reasoning`
are optional, retaining the original API body. Context changes restart the backend;
identical selections do not. Invalid settings are rejected before interrupting the
current selection. Normal engine changes still refuse busy or unknown occupancy.
For the currently loaded engine/model, omitted context or reasoning keeps its existing
selection. A different model inherits its configured defaults. Send an explicit capacity
or `"reasoning":"auto"` to reset that setting to a known value.

`GET /api/engines` exposes each model's `context_limits`, `reasoning_modes` and
`reasoning_change`. Legacy entries without explicit capabilities remain fixed. An
administrator must declare a supported binding before the UI offers an adjustment.
Use the example profiles as templates and check the installed engine's actual flags.

`GET /api/status` returns `active_settings` and `selection_id`. The context source says
whether capacity was read from native slots or declared in configuration. `/v1/models`
reports the loaded model's effective context when native readback is available. Slot
capacity does not measure engine RSS, and prompt-cache memory is a separate setting.
`active_settings.requested_context` retains the chosen launch capacity separately from
the effective `context`, so restoring the dashboard preserves the intended load settings.

Reasoning **Auto / On / Off** selects a default. Explicit client reasoning controls
override it. Supported request defaults can change without restarting weights; profiles
requiring different startup arguments restart. A model/template with fixed reasoning
does not advertise a toggle. `reasoning_format: none` only changes output parsing; it
does not disable thinking. Raw completion endpoints have no universal reasoning control.
Request-based On/Off defaults require an explicit mapping for the API endpoint being
used. An unmapped default returns 422 instead of silently using the backend's own mode;
choose Auto or supply a supported explicit client control. The sample Gufo profile maps
Chat, Responses and Messages. Other profiles expose only administrator-reviewed mappings.

Hermes may send `reasoning_effort: medium` even without a deliberate user selection;
that overrides the gateway's default. Set the intended effort in the client when needed.
Changing reasoning/template prefixes may invalidate prompt-cache reuse without reloading
weights. Do not replace custom templates until their tool-call behavior has been tested.

### Client budgets

Print a configuration fragment without changing the engine or client files:

```sh
inference-gateway client-config --url http://GATEWAY:9000 \
  --client opencode --model my-model --context 65536 --output-tokens 4096
inference-gateway client-config --url http://GATEWAY:9000 \
  --client hermes --model my-model --context 65536
```

These commands use `IGM_CONTROL_KEY` to inspect the catalog and active capacity. The
output references `IGM_INFERENCE_KEY`; it contains no credential value. Merge the JSON
fragment into the client's configuration (JSON also parses as YAML for Hermes).
Make the inference credential available to the harness through its environment or
credential manager. Never substitute the control key into an ordinary worker config.

OpenCode uses `limit.context`/`limit.output`. Hermes can discover `/v1/models` but its
explicit `model.context_length` pin and other metadata/cache sources can take precedence.
Restart or explicitly reconfigure the client after changing its budget; refreshing the
gateway catalog does not guarantee an existing session updates its resolved limit.
Compact or start a fresh conversation before reducing capacity below its existing history.
Input, tools, reasoning and output must fit the engine's capacity together.

## Password login and remembered browsers

Enable the optional browser account in the administrator configuration:

```json
"browser_auth": {
  "enabled": true,
  "allow_insecure_http_sessions": true
}
```

The HTTP opt-in is for a trusted LAN. HTTP exposes passwords and session cookies to
network interception. For HTTPS leave `allow_insecure_http_sessions` false or omitted.
For a **same-host TLS reverse proxy**, set `browser_auth.public_origin` to the exact
browser origin and add that origin to `allowed_origins`. Only loopback proxy connections
can use the configured public origin as their transport; forwarded headers are not trusted.
Preserve the existing Host/network restrictions and keep the backend private.

Set a password of at least 12 characters using the gateway-owning account:

```sh
inference-gateway set-password --state-dir /var/lib/inference-gateway-manager
```

Use the **same state directory as the running service**. The command prompts twice,
stores an Argon2id hash, and revokes all browser sessions. There is no default password
and no password command-line argument. Configuration changes require an ordinary
gateway restart; setting/resetting the password itself does not require a restart.

The dashboard supports password managers, password visibility, and **Remember this
browser**. A remembered session expires after 30 days or seven days of inactivity;
unchecked sessions last at most 12 hours. Refreshing the page or restarting the gateway
does not discard a valid remembered session. Logout and individual browser revocation
take effect server-side. The session database is private and holds hashed opaque tokens,
not the raw browser credential. Back up the state directory privately; its contents are
sensitive even though passwords and session tokens are hashed.

Control-key login remains available as an advanced option and is held only in memory.
API applications keep using Bearer keys. A browser cookie never authorizes `/v1` calls.
Cookie-authenticated mutations require an exact allowed Origin and `X-CSRF-Token` from
`GET /api/auth/session`. The dashboard handles these automatically.

Login is limited to ten attempts per source and sixty globally per minute, with at most
two password checks running at once. This limits costly verification work, but repeated
attempts can temporarily deny password login. A same-host TLS proxy shares one source
bucket across its browsers; untrusted forwarding headers do not change that identity.
Existing sessions and the control-key login/API remain available during login throttling.
Keep the control key privately available for operator recovery. Session activity is
persisted at most once a minute; database I/O runs outside the API event loop.
Storage timeouts return 503 with a safe session-store message; control-key access and
health checks remain independent of the browser session database.

Authentication endpoints:

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/auth/config` | Password-login availability, no credential values |
| POST | `/api/auth/login` | `{password, remember}`; issues HttpOnly cookie |
| GET | `/api/auth/session` | Restore session and obtain CSRF token |
| POST | `/api/auth/logout` | Revoke current browser session |
| GET | `/api/auth/sessions` | List browser sessions |
| DELETE | `/api/auth/sessions/{id}` | Revoke a browser session |

## Add models without editing executable configuration

An administrator first configures `model_roots` and engine `profiles`; see
[the example](../examples/config.example.json). Roots are a mapping of readable labels
to absolute directories on the **gateway host**, for example `{"weights":"/srv/models"}`.
Keep them narrow and do not include credential or application-state directories.

Profiles own commands, runtime environment, backend address, capabilities and required
artifact formats. The registry owns only model data. Registration cannot introduce an
executable, an arbitrary command, environment variables, or an upstream URL. Paths and
all discovered companions must remain inside approved roots; escaping symlinks and
special files are refused.

Existing models gain context controls by replacing their numeric launch-context argument
with `{context}` and adding validated `context_limits` matching the installed engine/model.
Keep every unrelated launch argument unchanged. For native configuration-file engines
such as Strata, use an administrator-owned inline `config_template` and `{config_path}`
in the outer launch command; the manager writes a private per-launch copy, substitutes
the validated settings, and removes the copy after verified shutdown. It never edits the
original engine configuration. The example includes the actual Strata dense/native pack
layout and its explicit native GGUF and tokenizer companions.

Generic GGUF registration offers Auto reasoning only. The separate hybrid-Qwen profile
is for reviewed compatible templates; selecting a profile is not an automatic proof that
every model with the same architecture supports that template's reasoning controls.

In the dashboard, choose an engine and launch profile, browse its model directory,
inspect the files, review the ID/settings and add the registration. Registration updates
the catalog immediately; it does not load a model or restart the gateway.

The same operation is available remotely:

```sh
inference-gateway models inspect --url http://GATEWAY:9000 \
  --engine llama --profile gguf --path /srv/models/example.gguf
inference-gateway models add --url http://GATEWAY:9000 \
  --engine llama --profile gguf --path /srv/models/example.gguf --model example
```

Use `--dry-run` with `models add` for a preview. Profiles needing companions accept
repeated `--artifact NAME=/absolute/host/path` arguments. For split GGUF choose the first
shard; every shard must exist. Kyojin needs an already converted compatible EXL3 pack.
Strata's pack/native/tokenizer combination is distinct from a generic safetensors model.
The inspector checks metadata and required files, never downloads, converts or executes
model code. Successful registration is not proof the model runs on the installed engine.

The HTTP interface is `GET /api/model-files`, `POST /api/models/inspect`, then
`POST /api/models` with the returned `candidate`. Inspection returns a sanitized summary
and warnings; saving revalidates the candidate. Registrations persist in `models.json`
inside the state directory with atomic writes. Back up this file with the configuration.
Duplicate model IDs are refused across engines. Static registrations remain supported.

Missing files, unavailable mounts, removed roots/profiles/engines, and static-ID conflicts
do not prevent startup. Such saved registrations appear in the catalog's
`unavailable_registrations`; when their engine and ID are available they also appear as
disabled models. They cannot be loaded. Restore the files/configuration and restart to
revalidate, or remove the saved registration:

```sh
inference-gateway models remove --url http://GATEWAY:9000 --engine llama --model example
```

This calls `DELETE /api/models/{engine}/{model}` with control authentication, atomically
removes only saved registration data and keeps all model files. Unload a loaded model
first. Static entries are edited in administrator configuration; removing a conflicting
saved entry leaves the static model intact. A structurally corrupt or unreadable registry
file still fails closed at startup: restore its private backup instead of overwriting it.
Startup logs identify the unavailable registration and validation failure without listing
its artifact path. Removal serializes its durable write with lifecycle operations; a new
inference request can briefly receive the ordinary lifecycle-busy 409 during that write.

## Gufo

The fifth engine is `gufo`. IGM launches and supervises a **natively installed** Gufo binary:
there is no container layer. The administrator-owned profile runs `gufo serve ... llm` on
loopback port 8083, with `--sessions 1`, `--context` and `--served-model-name`. Readiness
uses `/ready`; native occupancy uses `/slots`. Like every other engine, the process is a
direct child of the gateway, so ownership, unload and shutdown need no extra forwarding.

Build and install Gufo on the gateway host by following
[Native Gufo on the gateway host](gufo-native.md); upstream's own
[build instructions](https://github.com/gufo-org/gufo) are the authority for toolchain
requirements. Replace the placeholder executable/model paths in the example; do not
replace an existing private deployment configuration with the whole example. Supported
architectures and weight formats follow the installed Gufo release, not every GGUF accepted
by llama.cpp. The native matchers cover the `gufo` executable, the `gufo-server` alias and
the `.gufo-wrapped` name a Nix-built package uses. Preserve them when adapting the launch
command.

Two example profiles are included. `gguf` serves a single GGUF. `dflash2-gguf` also declares
two explicit companion artifacts, a vision projector (`mmproj`) and a DFlash2 draft model
(`draft`), and passes them with `--mmproj` and `--speculative dflash2 --dflash-model`. A
draft must be the file Gufo documents for that target; do not substitute a similarly named
draft. Register such a model with
`inference-gateway models add --engine gufo --profile dflash2-gguf --path TARGET.gguf --artifact mmproj=... --artifact draft=...`.

If the Gufo binary needs runtime libraries outside the system paths, link it with a fixed
`RUNPATH` or add an administrator-owned `env` mapping to the profile. Do not depend on the
gateway's own environment. The initial integration covers text, streaming, and
client-managed tool-call workflows. It does not add Gufo's audio, image-generation or
video APIs to IGM.

## MCP on another machine

Install the built wheel with the MCP extra on the **harness machine** (Python 3.11+):

```sh
python -m pip install 'inference_gateway_manager-0.1.0-py3-none-any.whl[mcp]'
```

Supply `IGM_CONTROL_KEY`, optionally a separate `IGM_INFERENCE_KEY`, and `IGM_URL` to
the process using your harness's credential/environment configuration. If no inference
key is supplied, the trusted controller uses its control key for delegation. Ordinary
workers should still receive only the inference key. Start the stdio process with:

```sh
igm-mcp --url http://GATEWAY:9000
# Equivalent:
python -m gateway.mcp --url http://GATEWAY:9000
```

The adapter binds no listening port. It needs outbound connectivity to IGM, not model
files, shared storage, engine binaries, SSH access or Linux process permissions. Windows
and macOS clients use the same entrypoint; real-device acceptance remains separate.
Credentials never belong in URLs or tool arguments. HTTP's interception limitation also
applies to this adapter; use an HTTPS gateway URL when available.

Tools: `gateway_catalog`, `gateway_status`, `gateway_telemetry`, `gateway_load`,
`gateway_unload`, `gateway_delegate`. Discover → load explicitly → preserve the returned
`selection_id` → delegate using that ID. Loading can take several minutes: configure
the harness's MCP timeout to allow the gateway's startup timeout (normally 900 seconds).
All lifecycle tools use `force:false`. There are no recovery, shell or registration tools.

Delegation is one bounded Chat Completions request using the supplied prompt and context,
with a default 4096-token response budget and a hard 16384-token adapter cap. Reasoning
counts toward that budget. It returns text, usage and any tool-call data, but does not
execute tools or act on the harness's workspace. Treat worker output as untrusted material
for the parent harness to review. The adapter does not retry or reload after a busy/stale
selection refusal. Its fixed gateway URL cannot be overridden through tool arguments.

MCP control/delegation does not configure the harness's primary inference provider. Use
`/v1` and the client fragment above for that role. Initial engine loading must happen
before an unloaded primary model can drive a harness to call tools.

## Deployment acceptance

Repository tests use disposable fixtures. Before replacing a production deployment,
back up its config/state, arrange an idle maintenance window, and verify each new
profile against the installed engine. Check native context readback, on/off reasoning
with supported templates, streaming/tool calls, busy refusal, full native process exit,
browser refresh/revocation, and a real MCP round trip from the second host. Rollback
must restore both compatible code and the previous config/state. No service restart or
model load is performed by the development workflow itself.

Run the following on an idle gateway host and a separate harness machine. Record the
commit, engine/client versions, chosen model, requested/effective context, selection ID
and pass/fail results. Keep credentials and browser-session data out of the evidence.

1. **Prepare and confirm baseline.** Wait for hosted Linux and portable-client CI.
   Privately back up the current code revision, configuration and state directory. Record
   the service account and actual state-directory path. Install the candidate build and
   add only the required configuration fields, preserving unrelated engine arguments.
   Configure approved model roots/profiles, set the browser password using the service's
   state directory, and install Gufo separately before enabling its example profile.
   Confirm health, control/inference key roles and normal generation still work.
2. **Check capacity and reasoning on real engines.** Use two supported capacities that
   fit the host (for example 65,536 and 262,144 only where supported). A capacity change
   must reload, update `requested_context` and native readback where available, and still
   generate. Repeating a load with omitted settings must retain the process and selection.
   Reopening the dashboard must retain the chosen capacity/reasoning. Verify Auto/On/Off
   only on a reviewed compatible template, including an explicit client override. Use
   backend metadata/usage where available instead of judging reasoning from answer style.
   Request-based defaults should change without a process restart; restart-based profiles
   must behave as declared. Unsupported endpoint mappings should refuse explicitly.
3. **Check remembered login.** Sign in with Remember enabled, refresh, close/reopen the
   browser and restart the gateway; the session should survive. Sign in from another
   browser, revoke it and confirm its next request fails. Logout and password reset must
   revoke access. A browser cookie alone must never authorize inference, and the existing
   control-key route must still work. Use HTTPS or the explicitly chosen LAN HTTP opt-in.
4. **Check registration and Gufo.** Add a supported model through the GUI and another
   through CLI inspection/dry-run/save. The catalog must update without loading weights;
   entries must survive restart. Removing a registration must retain its files, and removing
   a loaded model must refuse until unload. Use disposable files and a separate test state
   for the missing-artifact/restart drill: the gateway should start with that entry disabled.
   For installed Gufo, check `/ready`, idle/busy `/slots`, effective context, generation,
   streaming and one harmless client-managed tool call. Ordinary unload must leave no
   owned native process; verify another engine can load afterward. Repeat a short baseline
   generation/unload check for the other installed engines used in the deployment.
5. **Check Hermes/OpenCode budgets.** Use the inference key and exact loaded model ID.
   With the engine already at a larger capacity, start a fresh client task with a 65,536
   budget; the engine process/capacity must stay unchanged. Explicitly reload the engine
   smaller and restart/reconfigure the client, then verify both limits agree. Test ordinary
   generation, streaming and a harmless tool call in each harness. A client model picker
   must not implicitly load/switch an engine.
6. **Check MCP from the second host and rollback.** Install the wheel with `[mcp]`, set
   the fixed gateway URL and private environment credentials, and connect the stdio adapter
   in the real harness. Exercise catalog/status/telemetry, explicit load, a bounded delegate
   call with its selection ID, and unload. Busy and stale selections must refuse without
   force, retries or automatic reload. Returned tool calls must remain data in the adapter.
   Verify the saved code/config/state rollback procedure in the test environment before
   replacing the production installation. Record any untested engine/client combination
   explicitly; successful fixture or hosted CI runs do not certify those combinations.
