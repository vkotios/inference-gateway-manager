# Client setup

Load the model through the dashboard/control API before selecting it in a client. An inference request never implicitly starts or switches an engine. The inference host runs Linux; client machines can run Windows, macOS or Linux.

## Generic OpenAI-compatible client

| Setting | Value |
|---|---|
| Provider/transport | OpenAI-compatible **Chat Completions** |
| Base URL | `http://GATEWAY_HOST:9000/v1` |
| API key | Value of `IGM_INFERENCE_KEY` |
| Model | Exact enabled model ID discovered from `/v1/models` |

Replace `GATEWAY_HOST` with your host's reachable address/name. If the client separately asks for a complete endpoint, use `/v1/chat/completions`; do not append `/v1` twice. All configured models share the base URL/key, but only the loaded model can infer. A model picker does not perform lifecycle operations. `/v1/models` includes unloaded models: check its `status` field rather than assuming every listed model is ready.

The dashboard is `/`. `/v1` is not an interactive surface: a browser GET returning `{"detail":"Not Found"}` is expected and does not test inference. `/health` proves API liveness only, not model readiness.

## Credentials on the client machine

IGM's host-side `init-keys` writes a private credential file at the operator-selected location (the README uses `.local/credentials.env`). Transfer only the needed value through a trusted channel; do not share the entire file with workers. Both keys use `Authorization: Bearer KEY`. The control key also permits inference but should not be used as an ordinary client's API key.

Suggested environment convention:

| Variable | Consumer |
|---|---|
| `IGM_BASE_URL` | Controller or smoke test; gateway root without `/v1` |
| `IGM_INFERENCE_KEY` | Inference client/worker |
| `IGM_CONTROL_KEY` | Dedicated trusted controller only |

These variables are not automatically consumed by every application. Explicitly reference them in its provider/plugin configuration. Setting the control key does not install a controller or grant tools to an agent.

### Windows / PowerShell

Use the application's secret store when available. For a temporary PowerShell session, prompt for the inference key without putting it in command history:

```powershell
$env:IGM_BASE_URL = 'http://GATEWAY_HOST:9000'
$secret = Read-Host 'Inference key' -AsSecureString
$env:IGM_INFERENCE_KEY = [System.Net.NetworkCredential]::new('', $secret).Password
Remove-Variable secret
# Start your client from this shell so it inherits the variables.
```

For a controller, repeat the secure prompt separately and assign its value to `$env:IGM_CONTROL_KEY`; do not give that variable to inference-only workers. Once assigned, an environment value is plaintext inside the process, despite the masked prompt.

For persistence, Windows **Edit environment variables for your account** can set these as user variables. Restart the client/orchestrator afterward; an already-running app does not inherit changes. Persistent user variables are not encrypted and may be inherited by unrelated processes. Prefer process-scoped credentials or a dedicated secret store. Never use a literal key in a `setx` command, a screenshot, a repository file or a prompt.

### Linux / macOS

Set `IGM_BASE_URL` and have your secret manager supply the key to the intended process. In Bash, a temporary masked prompt is another option:

```bash
export IGM_BASE_URL='http://GATEWAY_HOST:9000'
read -r -s -p 'Inference key: ' IGM_INFERENCE_KEY; printf '\n'
export IGM_INFERENCE_KEY
# Start your client from this shell.
```

Avoid putting literal keys in shell history or committing environment files. LAN HTTP is unencrypted: use only a trusted network, SSH tunnel, or correctly configured TLS proxy.

## Authenticated smoke test

This Python 3 example works on Windows (`python`) and Linux/macOS (`python3`). It discovers the loaded model and sends one short, non-streaming request. Run only when you intend to consume the inference slot. It does not load or switch engines and prints neither credential nor server error bodies.

```python
import json
import os
import urllib.error
import urllib.request

base = os.environ['IGM_BASE_URL'].rstrip('/')
key = os.environ['IGM_INFERENCE_KEY']

def call(path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        base + path, data=data,
        headers={'Authorization': 'Bearer ' + key,
                 'Content-Type': 'application/json'},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise SystemExit(f'Gateway HTTP {error.code}; see troubleshooting.') from None
    except urllib.error.URLError:
        raise SystemExit('Gateway connection failed; check address/network.') from None

loaded = [m['id'] for m in call('/v1/models')['data']
          if m.get('status') == 'loaded']
if len(loaded) != 1:
    raise SystemExit('Load exactly one model through the dashboard/controller first.')
result = call('/v1/chat/completions', {
    'model': loaded[0],
    'messages': [{'role': 'user', 'content': 'Reply with a short greeting.'}],
    'max_tokens': 128,
    'stream': False,
})
print(result['choices'][0]['message']['content'])
```

The timeout and output length above are smoke-test choices, not model limits or lifecycle deadlines. A slow cold request may need a longer client timeout. Tool calls, streaming and coding-client endurance require separate tests per backend.

## Troubleshooting

| Result | Meaning / next step |
|---|---|
| GET `/v1` returns 404 | Wrong test surface; use authenticated `/v1/models` |
| 401 | Missing/invalid Bearer credential |
| 403 | Role, client network, Host or Origin rejected; inspect configuration, not just the key |
| 409 | Busy, lifecycle conflict, no loaded model or model mismatch; read status with a controller |
| 502 | Upstream connection/request failed; inspect backend readiness and logs |
| 503 | Backend not ready, startup/shutdown failure or unknown inference occupancy; inspect status/local logs |
| Connection refused/timeout | Check host service, reachable address, allowed network and firewall |
| Transport-specific 404/error | Backend may not support Responses or Messages; try Chat Completions |

Do not retry a request blindly after losing its connection: it may have already consumed work. See [controller coordination](orchestration.md) before building automated switching.

## OpenCode

Merge this provider into your existing `opencode.json`; keep other providers intact. Replace the example host. Set `IGM_INFERENCE_KEY` privately in the client's environment.

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "gateway": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "LAN Inference Gateway",
      "options": {
        "baseURL": "http://GATEWAY_IP:9000/v1",
        "apiKey": "{env:IGM_INFERENCE_KEY}"
      },
      "models": {
        "qwen3.8-flash-next": {
          "name": "Flash Next · Strata · 256K",
          "limit": {"context": 262144, "output": 8192}
        }
      }
    }
  }
}
```

Select `gateway/qwen3.8-flash-next` with `/models` or `opencode run --model gateway/qwen3.8-flash-next 'your prompt'`. Use `@ai-sdk/openai-compatible` (Chat Completions), not the Responses-default OpenAI adapter for Strata. 8192 is a conservative client output budget, not a newly measured engine limit. Input plus output must fit 262144.

## Hermes

For an optional session alias, merge the following **settings** into the active profile through `hermes config set` rather than editing YAML manually. Store the inference key in that profile's private environment file or process environment. Do not change another profile.

```yaml
model_aliases:
  strata-flash:
    model: qwen3.8-flash-next
    provider: custom
    base_url: http://GATEWAY_IP:9000/v1
    key_env: IGM_INFERENCE_KEY
```

Example setter (run in the intended profile; replace the example host):

```sh
hermes config set model_aliases.strata-flash '{"model":"qwen3.8-flash-next","provider":"custom","base_url":"http://GATEWAY_IP:9000/v1","key_env":"IGM_INFERENCE_KEY"}'
```

The configuration schema in some Hermes builds warns that `model_aliases` is unrecognized even though the native alias loader supports it. The setter was exercised in an isolated configuration and its persisted dictionary was accepted by that loader; this is not a full agent-run certification.

Then `/model strata-flash` is session-scoped. It need not replace your cloud conversational lead or overwrite a resumed session's deliberate model override.

To make it the primary default, use `hermes model` → Custom endpoint, or the supported config setter for `model.provider=custom`, `model.default=qwen3.8-flash-next`, `model.base_url=http://GATEWAY_IP:9000/v1`, and `model.api_key=${IGM_INFERENCE_KEY}`. Use Chat Completions transport if your version exposes `api_mode`. Client context accounting can be set to 262144 through `model.context_length` where supported; it does not change the native engine reservation.

Existing occupancy-aware Hermes admission routers that query llama.cpp's `/models` and dynamic native ports require adaptation to the manager's authenticated `/api/status` and `/v1/*`. Merely changing the URL is **not** sufficient for those routers. This repository deliberately does not ship your future orchestration plugin.

## Other AI sessions

Claude/Codex/OpenCode agents can manage the engine using HTTP control APIs independently of their own inference provider. Give such an agent the control credential only when authorized to interrupt/switch models. Requests with an inference credential cannot perform lifecycle actions. Backend-specific inference compatibility (particularly Responses and Anthropic Messages) must be tested separately; control-plane support is not a claim that every client can infer with every engine.

Sources: [OpenCode providers](https://opencode.ai/docs/providers/), [Hermes providers](https://hermes-agent.nousresearch.com/docs/integrations/providers).
