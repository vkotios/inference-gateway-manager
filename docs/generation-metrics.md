# Generation metrics

The gateway reports how fast the loaded model generates and prefills, live while a request
runs and for the last finished request. It observes the response stream passively: it
never changes, delays or buffers the bytes it forwards, never adds anything to your
request, and never stores prompt or completion text. State is in memory and resets when
the gateway restarts.

## What is reported

| Value | Source | Exact? |
| --- | --- | --- |
| Generation tokens/s | engine `timings.predicted_per_second` | yes |
| Prefill tokens/s | engine `timings.prompt_per_second` | yes |
| Cached prompt tokens | engine `timings.cache_n` | yes |
| Draft acceptance | engine `timings.draft_n`, `draft_n_accepted` | yes |
| Time to first token | gateway clock, request sent to first content event | measured |
| Live generation tokens/s | content events over the stream so far | estimate |
| Generation tokens/s, engine sent no timings | tokens over the first-to-last content event span | estimate |
| Prefill tokens/s, engine sent no timings | not reported (`null`) | n/a |

Prefill speed is never derived from time to first token: queueing and prompt caching make
that wrong. A fully cached prompt processes zero tokens and reports no prefill speed.
Unavailable values are `null`. llama.cpp, Strata and native Gufo all return `timings`.

A request is recorded only when the response was 2xx and produced content or timings;
errors and rejected requests leave the previous result in place.

## API

`GET /api/status` includes a `generation` object:

```json
"generation": {
  "live": {"state": "generating", "elapsed_seconds": 3.1, "tokens": 120, "tokens_per_second": 23.4},
  "last": {
    "finished_at": 1792668000.0, "model": "qwen38_gufo", "client": "hermes",
    "outcome": "complete", "source": "engine",
    "generation": {"tokens": 1219, "tokens_per_second": 25.6},
    "prefill": {"tokens": 2048, "tokens_per_second": 173.0, "cached_tokens": 0},
    "time_to_first_token_seconds": 11.9, "total_seconds": 59.4,
    "draft": {"proposed": 1578, "accepted": 990, "acceptance": 0.627}
  }
}
```

- `live` is `null` when nothing is running. `state` is `prefill` until the first content
  event, then `generating`. `tokens_per_second` is `null` until three content events.
- `last` is `null` until a request has finished. `outcome` is `complete`, `cancelled`
  (the client went away) or `error`. `source` is `engine` when the engine reported the
  generation speed and `estimated` otherwise; prefill values come from the engine only.
- `draft` is `null` unless the engine used speculative decoding.

## Dashboard

The Inference activity card shows one line, for example
`Generating · 23.4 tok/s · 120 tokens (estimate)` while running and
`Last: Generation 25.6 tok/s · Prefill 173 tok/s · first token 11.9 s · 1,219 tokens · 63% of drafts accepted`
afterwards. The page polls every few seconds, so the live figure updates in steps.

## Limits

Streams with lines over 1 MiB and plain JSON bodies over 4 MiB are not measured. The
live figure counts content events, so an engine that batches several tokens into one event
reads low until the final engine-reported figure arrives.
