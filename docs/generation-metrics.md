# Generation metrics

The gateway reports how fast the loaded model generates and prefills, live while a request
runs and for the last finished request. It observes the response stream passively (with optional read-only Lucebox status polling): it
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
Unavailable values are `null`. llama.cpp, Strata and native Gufo return top-level `timings`; Lucebox uses `usage.timings`.

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

- `live` is `null` when nothing is running. `state` is `waiting` while phase is unknown, then `generating` on content.
  With native Lucebox polling it follows the reported `prefill`/`decode` phase. `tokens_per_second` is `null` until three content events span at least 0.5 seconds,
  or two native decode counter samples are available.
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

## Lucebox

Lucebox reports final counters inside `usage.timings`, for both JSON and SSE responses.
The gateway uses `decode_tokens_per_sec`, `prefilled_tokens`, `prefill_ms`, and
`cached_prefix_tokens`. Prefill speed is the number actually processed divided by the
reported prefill duration; cached prompt tokens are excluded. Fully cached prompts keep
zero processed tokens and an unknown prefill speed. Speculative acceptance is reported
when Lucebox supplies it; missing draft proposal/acceptance counts remain unknown.

Enable native live phase/counter polling explicitly in a model's launch configuration:

```json
"generation_metrics": "lucebox"
```

This reads the configured loopback backend's `/status/json`, at most once per second,
with a 0.5-second timeout and a 4 MiB body limit. It does not follow redirects. Only the
current phase and numeric token/time counters are retained; the endpoint's prompt
excerpts, messages, and draft text are discarded. No inference request or setting is
changed. Native observations are used only for one attributable gateway request; idle,
aggregate, mismatched-model, stale, malformed, and unavailable snapshots are ignored.
The gateway rechecks request identity after polling to avoid showing a completed
request's counters on another selection. Lucebox does not include the gateway request
ID in this endpoint, so this feature assumes inference uses the gateway; direct backend
requests can make native counter attribution ambiguous.

Native decode is shown as `Generating` even when the response has not emitted any
content bytes yet. Live speed needs two decode counter samples and is labeled
`engine counters`; it can therefore show `measuring` on the first sample. An internal
pass/counter reset starts measurement again. Backend polling failures fall back to
stream observations. Until a phase is observable, the dashboard says
`Processing · phase unavailable`, rather than calling all waiting time prefill.

Lucebox tool-use checks may run internal nonstreaming passes and return a buffered
response, even when the client requested streaming. The selected response's final engine
timings are reported separately from the full client-observed duration, which can include
other passes. First-content arrival includes that buffering; it is not the instant the
backend began decoding. A response-delivery burst must not be interpreted as generation
speed: fallback delivery estimates require at least 0.5 seconds of observed content span.
