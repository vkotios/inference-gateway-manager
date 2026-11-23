# Chat admission: slots, queue and priority

A loaded chat model has a number of **chat slots**, set per model entry with `chat_slots` (default 1). The gateway admits one chat request per free slot. This page describes what happens when no slot is free, how interactive and batch work share the slots, and the operator actions on the dashboard.

Admission applies to chat inference (`/v1/chat/completions`, `/v1/completions`, `/v1/responses`, `/v1/messages`). Embeddings and transcription have their own handling.

## Modes

The slot count belongs to the **loaded model and its load configuration**, not to the engine. The mode is decided again on every load or switch:

| Mode | When | What a request gets when no slot is free |
|---|---|---|
| queued | `admission.mode` is `auto` (default) and the loaded model has more than one slot | It waits in a bounded priority queue (below). |
| single | the loaded model has one slot, or `admission.mode` is `single` | `409` at once, with `X-IGM-Code: engine_busy`. No queue, no classes. This is the behaviour before the broker existed. |

Capacity comes from the engine's own slot report where it has one (llama.cpp `/slots`, `probe: slots`). Otherwise (`probe: gateway_only`, for example Halogen Flash) it is `chat_slots` and the gateway counts its own admissions. Direct callers that bypass the gateway are not counted.

## Priority classes

Send `X-IGM-Priority: interactive` or `batch`. **A request without the header is `interactive`**, so existing clients are unchanged. Any other value is a `400` with `X-IGM-Code: bad_priority`.

- `interactive` may use every slot. It is served before queued batch requests and waits at most `interactiveTimeoutS`.
- `batch` leaves `reservedInteractive` slots free. It waits at most `batchTimeoutS`.

Within a class the queue is first in, first out. A new request never jumps a waiting request of the same or a higher class.

### Reserved slots and short-request borrowing

`reservedInteractive` is a **fixed number of slots**, not a fraction. It can follow a calendar, for example one slot in working hours and none overnight:

```json
"admission": {
  "reservedInteractive": {
    "default": 0,
    "timezone": "Europe/Athens",
    "windows": [
      {"days": ["mon", "tue", "wed", "thu", "fri"], "start": "08:00", "end": "20:00", "reserved": 1}
    ]
  }
}
```

A window whose `start` is later than its `end` runs overnight. A plain integer is also accepted. The value in force is never more than the slot count minus one, so batch always has at least one slot.

While a slot is reserved, a batch request may still use it if it is **expected to be short**, so an interactive request that arrives never waits behind a long generation. The expected length is the first of these that exists:

1. the request's own `max_tokens` (or `max_completion_tokens`);
2. the median of the last answers of the same client name (orch uses `orch/<run-id>`), once there are at least three;
3. the size class in `X-IGM-Size-Class: XS|S|M`, mapped through `sizeClassExpectedTokens`.

A request with none of these is treated as long. It is "short" when the expectation is at most `borrowShortTokens`. **Nothing is ever clamped or truncated**: the expectation only decides which slot a request may take, and a request that runs long is never cut off by the gateway.

## Refusals and headers

| Situation | Status | Headers |
|---|---|---|
| Waited, then admitted | normal response | `X-IGM-Queue-Wait-Ms` (only when it waited), `X-IGM-Priority` |
| Waited longer than the class limit | `429` | `X-IGM-Code: queue_timeout`, `Retry-After` |
| Queue already holds `maxQueue` requests | `429` | `X-IGM-Code: queue_full`, `Retry-After` |
| Dropped by the operator while waiting | `429` | `X-IGM-Code: dropped_by_operator`, `Retry-After` |
| Model unloaded or switched while waiting | `409` | `X-IGM-Code: lifecycle`, `Retry-After` |
| Aborted by the operator before the engine answered | `499` | `X-IGM-Code: aborted_by_operator` |
| Aborted by the operator mid-stream | the stream ends with an SSE `error` event | `{"error": {"type": "aborted_by_operator", "code": "aborted_by_operator", "message": "..."}}` |

`Retry-After` is the median length of recent generations, between 1 and 60 seconds. The maintenance lease and lifecycle checks are unchanged. A client that disconnects while queued leaves the queue at once.

## Configuration

`admission` can appear at the top level of the configuration (a default for every model) and in a model entry (which overrides it):

| Key | Default | Meaning |
|---|---|---|
| `mode` | `auto` | `auto` or `single` (see Modes) |
| `reservedInteractive` | `0` | integer, or a calendar as above |
| `maxQueue` | `32` | waiting requests accepted before `queue_full` |
| `interactiveTimeoutS` / `batchTimeoutS` | `120` / `900` | longest wait per class |
| `borrowShortTokens` | `512` | largest expected length that may borrow a reserved slot |
| `sizeClassExpectedTokens` | `{"XS": 256, "S": 1024, "M": 4096}` | expectation for each size class |

The values above are starting points; tune them from measured traffic. Settings saved from the dashboard are kept in `admission-state.json` in the state directory and apply at once; **Use configured settings** removes them again. The same file holds the batch pause and the reservation override.

## Dashboard and API

The **Chat admission** card shows one row per running request (client, class, whether it uses the reserved slot, elapsed time, tokens so far) and one per waiting request (position, wait, expected length), with counters and p95 waits. Actions:

- **Pause batch / Resume batch**: stop admitting batch requests; running ones finish; interactive is unaffected.
- **Reserve a slot today / Release reservation today**: override the calendar until local midnight. **Follow the calendar again** clears the override.
- **Abort…** a running request and **Drop…** a waiting one. Both require a reason, which is sent to the client and recorded. Aborting breaks the agent's current turn.

| Method and path | Purpose |
|---|---|
| `GET /api/admission` | the view shown on the card (also `admission` in `/api/status`) |
| `POST /api/admission/pause` `{"paused": bool}` | pause or resume batch |
| `POST /api/admission/reservation` `{"value": int or null}` | override or clear the reservation |
| `POST /api/admission/settings` `{"settings": {...} or null}` | save or clear this model's settings |
| `POST /api/admission/requests/{id}/abort` `{"reason": "..."}` | abort a running request |
| `POST /api/admission/queue/{id}/drop` `{"reason": "..."}` | drop a waiting request |
| `GET /api/admission/events?after=<cursor>&limit=<n>` | the event log |

All of these need the control credential (or a signed-in browser).

## Event log

Aborts, drops, queue timeouts, queue-full refusals, pauses, reservation overrides and settings changes are recorded in a bounded in-memory log (the last 2,000). Each event has a `seq`, time, action, request id, client name, priority and, for operator actions, the reason and who acted. `GET /api/admission/events` returns `boot_id`, `next` (the cursor to pass as `after`), `oldest` and the events after the cursor. A changed `boot_id` means the gateway restarted and the cursor starts again. A consumer such as orch reads this log on a timer, so the gateway never needs to call out.
