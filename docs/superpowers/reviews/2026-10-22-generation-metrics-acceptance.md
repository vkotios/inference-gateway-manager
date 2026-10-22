# Generation metrics acceptance

Date: 2026-10-22. Branch `feat/generation-metrics`.

## Automated

- Full suite: 265 tests pass, repeated runs stable; `ruff check`, `ruff format --check` and
  `git diff --check` clean.
- Unit (`tests/test_metrics.py`): engine timings, estimates without timings, usage token
  counts, chunk-split and CRLF streams, reasoning and tool-call deltas, live view gating,
  plain JSON, non-generation responses, cancelled streams, invalid and non-finite values,
  oversized lines, garbage input, no stored text.
- Proxy (`tests/test_metrics_api.py`): forwarded bytes identical to the backend's, streamed
  and non-streamed reporting, other endpoints, rejected requests leave the last result,
  live view during a slow stream (a request in flight, queried through `/api/status`).
- Dashboard: wording for every state run through Node against the shipped `app.js`, and the
  line rendering in real Chromium after sign-in.

## Against a real engine

llama-server b10902 (Ornith, MTP drafting) on port 8080 was idle, so one 48-token streamed
and one non-streamed request were sent to it directly and the responses replayed through
the tap:

| | Engine reported | Tap reported |
| --- | --- | --- |
| Generation (stream) | 73.876 tok/s, 48 tokens | 73.9 tok/s, 48 tokens |
| Prefill (stream) | 140.81 tok/s, 19 tokens | 140.8 tok/s, 19 tokens |
| Drafts (stream) | 35 proposed, 29 accepted | 35 / 29, 0.829 |
| JSON body | 75.19 tok/s; prompt_n 4, cache_n 15 | 75.2 tok/s; 4 tokens, 15 cached |

Findings: llama.cpp streams `timings` in the last event without `stream_options`, so the
gateway needs no request change. The JSON case shows why prefill speed is only taken from
the engine: with 15 of 19 prompt tokens cached, only 4 were processed.

## Not verified

- The live (estimated) figure and time to first token against a real engine through the
  real gateway: an isolated gateway refuses to load while the production engine runs, so
  this is checked after the production update.
- Strata and native Gufo streams were not captured now; their earlier qualification runs
  recorded the same `timings` fields.
