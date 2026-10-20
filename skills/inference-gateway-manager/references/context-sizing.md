# Context sizing for coding workers

The parent Claude/Codex session chooses the worker's context budget before launch.
This is a planning policy, not an automatic gateway scheduler or a performance
guarantee. Use it with the companion skill's lifecycle and ownership checks.

## Separate three quantities

| Quantity | Meaning | How it changes |
|---|---|---|
| Backend capacity `B` | Effective per-request window actually served by the loaded engine | Explicit supported gateway load/settings change; context changes reload |
| Client window `C` | Budget advertised to OpenCode/Hermes for accounting and compaction | Configure the worker; keep `C <= B` |
| Actual live tokens | Instructions, schemas, source excerpts, history, tool results and generation | Changes as the task proceeds |

Read `active_settings.context` after readiness. A requested launch value or catalog
default alone is not proof of effective per-slot capacity. Unknown effective
capacity is a blocker for claiming that a chosen budget fits. A warm 256k backend
can serve a 64k client without a reload or separate model entries. Do not send a
new smaller `context` to `gateway_load` merely to lower the client's budget.

Capacity can affect engine memory allocation and performance. Actual prompt length,
cache reuse, generated tokens, reasoning and engine implementation also affect
latency. Merely advertising 64k instead of 256k does not shorten the same prompt or
guarantee faster generation. Repeated compaction adds summarization, lost detail,
source rereads and wall time. Optimize useful task completion, not smallest window.

## Repeatable selection procedure

1. **Bound the next milestone.** Name a concrete result and check, such as changing
   one API and passing its focused tests. Split unrelated work into separate workers
   rather than loading the whole repository into one conversation.
2. **Forecast the live working set.** Estimate `F` for system instructions, tool
   schemas and task brief; `A` for source/docs/diffs actually needed; and `G` for
   conversation, tool results, edits and verification through that milestone.
   Set `P = F + A + G`. Use the model's tokenizer or observed provider token counts
   where available. A text-length heuristic is a rough fallback, especially for
   code or non-English text; record the uncertainty. File count alone is inadequate.
3. **Allow uncertainty and generation.** Start with `P_safe = ceil(1.25 * P)` as a
   tunable planning margin. Set a separate generation cap `O`, including any
   reasoning tokens counted by this backend. Obtain the installed client's usable
   threshold `U(C)` after its output/compaction reserves. Choose the smallest
   practical supported client window satisfying `P_safe < U(C)` and `C <= B`.
   Do not subtract a reserve twice. If client accounting is unknown, start
   conservatively, label the estimate and verify usage before a long run.
4. **Reuse or load.** Prefer a matching warm model whose effective capacity covers
   `C`. If a load is needed, select a supported backend capacity covering `C`,
   respecting declared limits and host resources. Read back readiness/settings and
   selection ID. Lower backend capacity only for a measured resource reason at an
   idle boundary; account for restart and cache loss.
5. **Configure and launch the worker.** Export an exact-model OpenCode provider
   fragment with `--context C --output-tokens O` and the inference key reference.
   The exporter does not add a selection header: add `X-IGM-Selection-ID` with the
   observed selection ID under `provider.igm.options.headers`. Confirm the
   installed client's compaction settings. Keep
   automatic compaction enabled. The parent terminal launches OpenCode; MCP
   `gateway_delegate` cannot launch a coding session or execute tools.
6. **Measure and revise.** Record actual token usage, compactions, source rereads,
   generation length, elapsed time and the milestone's test outcome. Two compactions
   before the first milestone, or repeated compaction with little useful work
   between them, trigger reassessment. Checkpoint progress; trim irrelevant/tool
   output, split the task, or increase `C` within `B`. If `B` must grow, finish or
   stop the worker cleanly and coordinate an idle reload before a new session.
   Never resize a shared backend beneath an active worker or force past busy state.

The 25% margin and reassessment trigger are initial policy choices, not measured
model properties. Tune them from comparable completed tasks. Compaction during a
long successful task is normal; repeated early compaction is the warning sign.

## Worked OpenCode example

For the OpenCode 1.18.31 custom-provider path with no separate `limit.input`, its
[overflow implementation](https://github.com/anomalyco/opencode/blob/v1.18.31/packages/opencode/src/session/overflow.ts)
computes usable capacity as `limit.context - maxOutputTokens(...)`. With an effective
output maximum of 8,192, take `U(C) = C - 8,192`; confirm the installed version and
effective maximum rather than assuming all clients use this formula. When
`limit.input` exists, that version takes a different branch with a reserved buffer.

Suppose `F = 6,000`, `A = 18,000`, `G = 18,000`: `P = 42,000` and
`P_safe = 52,500`. A 65,536-token window gives `U = 57,344`, so 64k is a reasonable
starting client budget. With a warm effective `B = 262,144`, reuse it; do not reload
at 64k. If expected growth instead becomes 45,000, `P_safe = 86,250`: 64k no longer
fits, while 131,072 gives `U = 122,880`. Pick 128k or reduce the milestone.

Here “64k/128k/256k” means 65,536/131,072/262,144 tokens. These are examples, not
universal supported capacities. Hermes and other OpenCode versions can use different
accounting and compaction policies; inspect their installed configuration/source
and actual usage. Do not copy settings from a different major version blindly.

## Parent's launch note

Record this concise plan before dispatch; no credentials or private absolute paths:

```text
Milestone and verification:
Engine/model; reasoning mode and compatibility evidence:
Forecast F + A + G; estimate source/confidence:
Generation cap O; client usable-threshold rule:
Chosen C; observed effective B; warm reuse or idle load:
Selection ID; exact-model worker configuration:
Checkpoint/reassessment trigger; fallback if capacity is insufficient:
```

After completion, append observed usage/compactions, wall time and verification.
Report unavailable measurements as unknown. Never claim that a policy estimate is
a benchmark or that a text-only MCP answer proves OpenCode tool compatibility.
