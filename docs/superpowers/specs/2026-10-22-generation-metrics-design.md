# Generation metrics design

Approved in chat on 2026-10-22 by the operator: report generation and prefill speed,
built before the next gateway update, on its own branch.

The gateway proxies every inference call as raw bytes. This feature observes that stream
passively and reports how fast the loaded model generates and prefills, live while a
request runs and for the last finished request.

## Which metrics are reportable

| Metric | Source | Exact? |
| --- | --- | --- |
| Generation tok/s | engine `timings.predicted_per_second` (tokens `predicted_n`) | yes |
| Prefill tok/s | engine `timings.prompt_per_second` (tokens `prompt_n`) | yes |
| Cached prompt tokens | engine `timings.cache_n` | yes |
| Draft acceptance | engine `timings.draft_n`, `draft_n_accepted` | yes |
| Time to first token | gateway clock, request sent to first content event | measured |
| Generation tok/s, live | gateway clock over streamed content events | estimate |
| Generation tok/s, no timings | content events (or `usage.completion_tokens`) over the stream span | estimate |
| Prefill tok/s, no timings | not reported (`null`); time to first token still is | n/a |

Prefill speed is never derived from time to first token: queueing and prompt caching
make that number wrong. Unavailable values are `null`, never invented. `prompt_n` counts
only tokens actually processed; a fully cached prompt reports no prefill speed.

Measured on this host: llama.cpp, Strata and native Gufo all return `timings`.

## Behaviour

- The tap sits in `proxy()`'s response generator. It never alters, delays or buffers the
  bytes it forwards. Any parsing problem disables the tap for that request.
- It keeps counters and numbers only, never prompt or completion text.
- Server-sent events: each `data:` line is parsed as JSON. A `choices[].delta` with
  non-empty `content`, `reasoning_content` or `tool_calls` is one content event. A top
  level `timings` or `usage` object is remembered. Lines over 1 MiB disable the tap.
- Plain JSON responses (non-streaming): up to 4 MiB is kept to read `timings`/`usage`.
- A request is recorded only if the response was 2xx and produced content events or
  `timings`; embeddings, errors and model listings leave the last result untouched.
- Cancelled or failed streams are recorded with `outcome` set accordingly.
- State is in memory: it resets on gateway restart.

## API

`GET /api/status` gains a `generation` object (inference and control keys already see
status; it carries no content):

    "generation": {
      "live": null | {"state": "prefill"|"generating", "elapsed_seconds": 3.1,
                      "tokens": 120, "tokens_per_second": 23.4},
      "last": null | {
        "finished_at": <epoch s>, "model": "qwen38_gufo", "client": "hermes",
        "outcome": "complete"|"cancelled"|"error",
        "source": "engine"|"estimated",
        "generation": {"tokens": 1219, "tokens_per_second": 25.6},
        "prefill": {"tokens": 2048, "tokens_per_second": 173.0, "cached_tokens": 0},
        "time_to_first_token_seconds": 11.9, "total_seconds": 59.4,
        "draft": null | {"proposed": 1578, "accepted": 990, "acceptance": 0.63}
      }
    }

Live tokens/s needs at least three content events; before that it is `null`.

## Dashboard

The Inference activity card shows a metrics line: while running, "Generating · 23.4 tok/s
· 120 tokens (estimate)" or "Prefill · 3.1 s"; when idle, the last request: "Last:
generation 25.6 tok/s · prefill 173 tok/s · first token 11.9 s · 1,219 tokens", with
"(estimated)" when the source is not the engine. "No requests since start" otherwise.

## Out of scope

Persistence, per-client aggregation, history charts, and any change to the request
body (the gateway does not inject `stream_options`).
