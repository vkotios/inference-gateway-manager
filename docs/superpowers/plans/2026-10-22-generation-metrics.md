# Generation Metrics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox tracking.

**Goal:** Report generation and prefill speed, live and for the last request, from the
proxied response stream.
**Architecture:** `gateway/metrics.py` holds a per-request `Tap` (incremental SSE/JSON
reader) and a `Metrics` holder. `proxy()` feeds the tap from its response generator and
`/api/status` exposes the result; the dashboard renders one line.
**Spec:** docs/superpowers/specs/2026-10-22-generation-metrics-design.md

## Global Constraints

- Passive: never change, delay or buffer forwarded bytes; swallow tap errors.
- No prompt or completion text is stored; absent values are `null`.
- Preserve existing API bodies (additive `generation` key only).
- Test first; run the complete suite and ruff before completion.

### Task 1: Tap and summary
**Files:** gateway/metrics.py, tests/test_metrics.py
- [ ] Failing tests: SSE with timings, SSE without timings, split chunks, JSON body,
      invalid values, oversized lines, cancelled stream, no-content responses.
- [ ] Implement `Tap.feed/finish/live` and `Metrics.record/view`.

### Task 2: Proxy and status integration
**Files:** gateway/app.py, tests/fixture_backend.py, tests/test_metrics_api.py
- [ ] Failing tests through the app: streamed request, non-streamed, live view during a
      slow stream, embeddings leave last untouched, bytes forwarded identical.
- [ ] Feed the tap in `proxy()`; add `generation` to `/api/status`.

### Task 3: Dashboard and docs
**Files:** gateway/static/*, docs/generation-metrics.md, README.md, tests/test_dashboard.py
- [ ] Metrics line in the Inference activity card; docs; static test.

### Task 4: Acceptance
- [ ] Isolated gateway on a spare port with a real engine if memory allows; otherwise the
      fixture backend, and say which. Record in docs/superpowers/reviews/.
