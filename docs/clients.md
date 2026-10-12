# Coding clients

Load the model through the dashboard/control API before selecting it in a client. An inference request never implicitly starts or switches an engine.

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
