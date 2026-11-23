"""Chat admission: a bounded priority queue in front of the loaded model's chat slots.

Requests are admitted per request, never per run. ``interactive`` requests (the default when
no ``X-IGM-Priority`` header is sent) may use every slot. ``batch`` requests leave
``reservedInteractive`` slots free, except that a request expected to be short may borrow them.
Nothing is ever truncated or clamped: the expected length only decides which slot a request may
take, never how long it may run.

All state lives on the event loop; only ``acquire`` awaits.
"""

import asyncio
import collections
import contextlib
import datetime
import json
import logging
import os
import statistics
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger("gateway.admission")

INTERACTIVE = "interactive"
BATCH = "batch"
PRIORITIES = (INTERACTIVE, BATCH)
SIZE_CLASSES = ("XS", "S", "M")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
MODES = ("auto", "single")

DEFAULTS = {
    "mode": "auto",
    "reservedInteractive": 0,
    "maxQueue": 32,
    "interactiveTimeoutS": 120,
    "batchTimeoutS": 900,
    "borrowShortTokens": 512,
    "sizeClassExpectedTokens": {"XS": 256, "S": 1024, "M": 4096},
}
SETTING_KEYS = frozenset(DEFAULTS)
MAX_REASON = 500
EVENT_LIMIT = 2000
WAIT_SAMPLES = 200
HISTORY_SAMPLES = 20
HISTORY_MIN = 3
HISTORY_CLIENTS = 256
TICK_SECONDS = 0.5


class AdmissionRefused(Exception):
    """A refusal that the HTTP layer turns into a status, a code and optional headers."""

    def __init__(self, status, code, detail, retry_after=None):
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail
        self.retry_after = retry_after

    def headers(self):
        headers = {"X-IGM-Code": self.code}
        if self.retry_after is not None:
            headers["Retry-After"] = str(self.retry_after)
        return headers


def _int(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"admission {name} must be an integer from {low} to {high}")
    return value


def _clock_time(value, name):
    try:
        hours, minutes = value.split(":")
        parsed = datetime.time(int(hours), int(minutes))
    except (AttributeError, ValueError, TypeError):
        raise ValueError(f"admission {name} must be a HH:MM time") from None
    return parsed


def validate_reserved(value):
    """``reservedInteractive``: a fixed integer, or a default plus calendar windows."""
    if type(value) is int:
        _int(value, "reservedInteractive", 0, 64)
        return
    if not isinstance(value, dict) or set(value) - {"default", "windows", "timezone"}:
        raise ValueError("admission reservedInteractive must be an integer or a calendar object")
    _int(value.get("default", 0), "reservedInteractive default", 0, 64)
    timezone = value.get("timezone")
    if timezone is not None:
        try:
            ZoneInfo(timezone)
        except (KeyError, ValueError, TypeError, OSError):
            raise ValueError("admission calendar timezone is not a known zone") from None
    windows = value.get("windows", [])
    if not isinstance(windows, list) or len(windows) > 32:
        raise ValueError("admission calendar windows must be a list of at most 32")
    for window in windows:
        if not isinstance(window, dict) or set(window) - {"days", "start", "end", "reserved"}:
            raise ValueError("admission calendar window has unknown fields")
        days = window.get("days", list(DAYS))
        if not isinstance(days, list) or not days or any(d not in DAYS for d in days):
            raise ValueError("admission calendar days must be from mon to sun")
        _clock_time(window.get("start"), "calendar start")
        _clock_time(window.get("end"), "calendar end")
        _int(window.get("reserved"), "calendar reserved", 0, 64)


def validate_settings(raw, *, partial=False):
    """Validate an ``admission`` block. ``partial`` allows a subset (config layers, overrides)."""
    if not isinstance(raw, dict):
        raise ValueError("admission settings must be an object")
    unknown = set(raw) - SETTING_KEYS
    if unknown:
        raise ValueError("Unknown admission setting: " + ", ".join(sorted(map(str, unknown))))
    merged = {**DEFAULTS, **raw} if not partial else raw
    if "mode" in merged and merged["mode"] not in MODES:
        raise ValueError("admission mode must be auto or single")
    if "reservedInteractive" in merged:
        validate_reserved(merged["reservedInteractive"])
    if "maxQueue" in merged:
        _int(merged["maxQueue"], "maxQueue", 0, 1024)
    for key in ("interactiveTimeoutS", "batchTimeoutS"):
        if key in merged:
            _int(merged[key], key, 1, 86400)
    if "borrowShortTokens" in merged:
        _int(merged["borrowShortTokens"], "borrowShortTokens", 0, 1_000_000)
    if "sizeClassExpectedTokens" in merged:
        sizes = merged["sizeClassExpectedTokens"]
        if not isinstance(sizes, dict) or set(sizes) - set(SIZE_CLASSES):
            raise ValueError("admission sizeClassExpectedTokens keys must be XS, S or M")
        for tokens in sizes.values():
            _int(tokens, "sizeClassExpectedTokens", 1, 1_000_000)
    return raw


def resolve_settings(*layers):
    """Merge defaults and layers, lowest priority first. Missing layers are skipped."""
    out = json.loads(json.dumps(DEFAULTS))
    for layer in layers:
        if not layer:
            continue
        for key, value in layer.items():
            if key == "sizeClassExpectedTokens":
                out[key] = {**out[key], **value}
            else:
                out[key] = value
    return out


def reserved_at(policy, now):
    """Reserved interactive slots for an aware ``datetime``, from a fixed value or calendar."""
    if type(policy) is int:
        return policy
    zone = policy.get("timezone")
    local = now.astimezone(ZoneInfo(zone)) if zone else now.astimezone()
    for window in policy.get("windows", []):
        if DAYS[local.weekday()] not in window.get("days", DAYS):
            continue
        start, end = _clock_time(window["start"], "start"), _clock_time(window["end"], "end")
        moment = local.time()
        inside = start <= moment < end if start < end else moment >= start or moment < end
        if inside:
            return window["reserved"]
    return policy.get("default", 0)


@dataclass
class Grant:
    """One admitted request. ``release`` is idempotent."""

    id: str
    priority: str
    client: str
    borrowed: bool
    started: float
    started_wall: float
    waited_ms: int
    expected_tokens: int | None
    expected_source: str
    abort_event: asyncio.Event = field(default_factory=asyncio.Event)
    abort_reason: str | None = None
    tap: object = None
    released: bool = False

    @property
    def aborted(self):
        return self.abort_event.is_set()


@dataclass
class Entry:
    id: str
    priority: str
    client: str
    enqueued: float
    enqueued_wall: float
    seq: int
    expected_tokens: int | None
    expected_source: str
    short: bool
    future: asyncio.Future
    dropped: tuple | None = None


@dataclass(frozen=True)
class Capacity:
    """The loaded model's chat capacity. ``native_in_use`` is the engine's own count, if known."""

    total: int
    native_in_use: int | None = None


class AdmissionStore:
    """Operator overrides that survive a restart: pause, reservation override, settings."""

    def __init__(self, path):
        self.path = Path(path) if path else None
        self.data = {"paused": False, "override": None, "settings": {}}
        self.error = None
        if self.path and self.path.exists():
            try:
                loaded = json.loads(self.path.read_text("utf-8"))
                if not isinstance(loaded, dict):
                    raise ValueError("not an object")
                self.data["paused"] = loaded.get("paused") is True
                override = loaded.get("override")
                if isinstance(override, dict) and type(override.get("value")) is int:
                    self.data["override"] = {
                        "value": override["value"],
                        "until": float(override.get("until", 0)),
                    }
                settings = loaded.get("settings")
                if isinstance(settings, dict):
                    for block in settings.values():
                        validate_settings(block, partial=True)
                    self.data["settings"] = settings
            except (OSError, ValueError, TypeError) as exc:
                self.error = f"Ignored unreadable admission state: {exc}"
                log.warning(self.error)

    def save(self):
        if self.path is None:
            return
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1), "utf-8")
        os.replace(tmp, self.path)


class Admission:
    def __init__(self, state_path=None, *, clock=time.monotonic, wall=time.time, now=None):
        self.clock = clock
        self.wall = wall
        self.now = now or (lambda: datetime.datetime.now(datetime.timezone.utc))
        self.store = AdmissionStore(state_path)
        self.boot_id = uuid.uuid4().hex[:12]
        self.queue = []
        self.running = {}
        self.seq = 0
        self.event_seq = 0
        self.events = collections.deque(maxlen=EVENT_LIMIT)
        self.waits = {p: collections.deque(maxlen=WAIT_SAMPLES) for p in PRIORITIES}
        self.counters = {
            p: {"admitted": 0, "waited": 0, "timeouts": 0, "queue_full": 0, "dropped": 0}
            for p in PRIORITIES
        }
        self.aborted = 0
        self.history = collections.OrderedDict()
        self.durations = collections.deque(maxlen=50)
        self.capacity = Capacity(1)
        self.policy = resolve_settings()
        self.ticker = None
        self.reader = None
        self.model_key = None

    # ---- configuration -------------------------------------------------------------------

    def configure(self, model_key, config_layers, capacity, reader=None):
        """Called on every request: the loaded model decides settings and capacity (D8)."""
        if model_key != self.model_key:
            self.model_key = model_key
        self.policy = resolve_settings(*config_layers, self.store.data["settings"].get(model_key))
        self.capacity = capacity
        if reader is not None:
            self.reader = reader

    def strict(self):
        """Strict mode keeps the single-slot refusal: forced, or the model has one slot."""
        return self.policy["mode"] == "single" or self.capacity.total <= 1

    def reserved_now(self):
        override = self.store.data["override"]
        if override and override["until"] > self.wall():
            value = override["value"]
        else:
            if override:
                self.store.data["override"] = None
            value = reserved_at(self.policy["reservedInteractive"], self.now())
        return max(0, min(value, self.capacity.total - 1))

    # ---- admission -----------------------------------------------------------------------

    def expected_length(self, body_tokens, client, size_class):
        """Expected output tokens and where the estimate came from (D4, section 3.1)."""
        if type(body_tokens) is int and body_tokens > 0:
            return body_tokens, "request"
        samples = self.history.get(client)
        if samples and len(samples) >= HISTORY_MIN:
            return int(statistics.median(samples)), "history"
        sizes = self.policy["sizeClassExpectedTokens"]
        if size_class in sizes:
            return sizes[size_class], "size_class"
        return None, "unknown"

    def eligible(self, entry):
        """Return None if the entry may not start now, else True for a borrowed slot or False."""
        total = self.capacity.total
        in_use = max(len(self.running), self.capacity.native_in_use or 0)
        if in_use >= total:
            return None
        if entry.priority == INTERACTIVE:
            return False
        if self.store.data["paused"]:
            return None
        reserved = self.reserved_now()
        ordinary = sum(1 for g in self.running.values() if g.priority == BATCH and not g.borrowed)
        if ordinary < total - reserved:
            return False
        borrowed = sum(1 for g in self.running.values() if g.borrowed)
        if entry.short and borrowed < reserved:
            return True
        return None

    def grant_for(self, entry, borrowed):
        now = self.clock()
        waited = int((now - entry.enqueued) * 1000)
        grant = Grant(
            id=entry.id,
            priority=entry.priority,
            client=entry.client,
            borrowed=borrowed,
            started=now,
            started_wall=self.wall(),
            waited_ms=waited,
            expected_tokens=entry.expected_tokens,
            expected_source=entry.expected_source,
        )
        self.running[entry.id] = grant
        counters = self.counters[entry.priority]
        counters["admitted"] += 1
        if waited > 0:
            counters["waited"] += 1
        self.waits[entry.priority].append(waited)
        return grant

    def make_entry(self, request_id, priority, client, body_tokens, size_class):
        tokens, source = self.expected_length(body_tokens, client, size_class)
        short = tokens is not None and tokens <= self.policy["borrowShortTokens"]
        self.seq += 1
        future = asyncio.get_running_loop().create_future()
        # A refusal nobody awaits (the client left first) must not log "never retrieved".
        future.add_done_callback(lambda f: f.cancelled() or f.exception())
        return Entry(
            id=request_id,
            priority=priority,
            client=client,
            enqueued=self.clock(),
            enqueued_wall=self.wall(),
            seq=self.seq,
            expected_tokens=tokens,
            expected_source=source,
            short=short,
            future=future,
        )

    def refuse_busy(self):
        return AdmissionRefused(
            409, "engine_busy", "Engine already processing; retry when a chat slot is free"
        )

    def retry_after(self):
        if not self.durations:
            return 5
        return max(1, min(60, round(statistics.median(self.durations))))

    async def acquire(self, request_id, priority, client, body_tokens=None, size_class=None):
        """Wait for a slot. Raises ``AdmissionRefused``; cancellation drops the queue entry."""
        entry = self.make_entry(request_id, priority, client, body_tokens, size_class)
        if self.strict():
            # Today's refusal: no queue, no classes, no reservation; only a free slot admits.
            if max(len(self.running), self.capacity.native_in_use or 0) >= self.capacity.total:
                raise self.refuse_busy()
            return self.grant_for(entry, False)
        if not self.blocked_by_queue(entry):
            borrowed = self.eligible(entry)
            if borrowed is not None:
                return self.grant_for(entry, borrowed)
        if len(self.queue) >= self.policy["maxQueue"]:
            self.counters[priority]["queue_full"] += 1
            self.emit("queue_full", entry, depth=len(self.queue))
            raise AdmissionRefused(
                429, "queue_full", f"Queue is full ({len(self.queue)} waiting)", self.retry_after()
            )
        self.queue.append(entry)
        self.ensure_ticker()
        self.dispatch()
        timeout = self.policy["interactiveTimeoutS" if priority == INTERACTIVE else "batchTimeoutS"]
        try:
            return await asyncio.wait_for(asyncio.shield(entry.future), timeout)
        except asyncio.TimeoutError:
            self.remove(entry)
            if entry.future.done() and not entry.future.cancelled():
                if entry.future.exception() is None:  # admitted in the same tick as the timeout
                    return entry.future.result()
            self.counters[priority]["timeouts"] += 1
            self.emit("queue_timeout", entry, waited_ms=int((self.clock() - entry.enqueued) * 1000))
            raise AdmissionRefused(
                429,
                "queue_timeout",
                f"Waited {timeout} s for a chat slot ({priority})",
                self.retry_after(),
            ) from None
        except asyncio.CancelledError:
            self.remove(entry)
            if entry.future.done() and not entry.future.cancelled():
                if entry.future.exception() is None:
                    self.release(entry.future.result())
            raise

    def blocked_by_queue(self, entry):
        """An arrival never jumps a waiting entry of the same or higher class."""
        for waiting in self.queue:
            if waiting.priority == entry.priority or waiting.priority == INTERACTIVE:
                return True
        return False

    def ordered(self):
        return sorted(self.queue, key=lambda e: (PRIORITIES.index(e.priority), e.seq))

    def dispatch(self):
        """Start every queued entry that may start now. Cheap; safe to call at any time."""
        if not self.queue:
            return
        for entry in self.ordered():
            if entry.future.done():
                continue
            if entry.dropped is not None:
                self.queue.remove(entry)
                reason, retry = entry.dropped
                entry.future.set_exception(
                    AdmissionRefused(
                        429, "dropped_by_operator", f"Dropped by the operator: {reason}", retry
                    )
                )
                continue
            borrowed = self.eligible(entry)
            if borrowed is None:
                continue
            self.queue.remove(entry)
            entry.future.set_result(self.grant_for(entry, borrowed))

    def remove(self, entry):
        if entry in self.queue:
            self.queue.remove(entry)

    def release(self, grant, tokens=None):
        if grant is None or grant.released:
            return
        grant.released = True
        self.running.pop(grant.id, None)
        self.durations.append(max(0.0, self.clock() - grant.started))
        if tokens:
            samples = self.history.setdefault(
                grant.client, collections.deque(maxlen=HISTORY_SAMPLES)
            )
            self.history.move_to_end(grant.client)
            samples.append(int(tokens))
            while len(self.history) > HISTORY_CLIENTS:
                self.history.popitem(last=False)
        self.dispatch()

    def release_id(self, request_id):
        self.release(self.running.get(request_id))

    # ---- background tick -----------------------------------------------------------------

    def ensure_ticker(self):
        if self.ticker is None or self.ticker.done():
            self.ticker = asyncio.get_running_loop().create_task(self.tick())

    async def tick(self):
        """While anyone waits, re-read capacity and the calendar; engines can free slots silently."""
        try:
            while self.queue:
                await asyncio.sleep(TICK_SECONDS)
                if self.reader is not None:
                    try:
                        self.capacity = await self.reader()
                    except Exception:
                        log.warning("capacity read failed", exc_info=True)
                self.dispatch()
        except asyncio.CancelledError:
            pass

    async def close(self):
        if self.ticker is not None and not self.ticker.done():
            self.ticker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.ticker
        for entry in list(self.queue):
            if not entry.future.done():
                entry.future.set_exception(
                    AdmissionRefused(409, "lifecycle", "Gateway is shutting down", 5)
                )
        self.queue.clear()

    def reject_queued(self, status, code, detail, retry_after=5):
        """Lifecycle operation or model switch: queued requests get the existing refusal."""
        for entry in list(self.queue):
            if not entry.future.done():
                entry.future.set_exception(AdmissionRefused(status, code, detail, retry_after))
        self.queue.clear()

    # ---- operator actions ----------------------------------------------------------------

    def emit(self, action, entry_or_grant=None, **fields):
        self.event_seq += 1
        item = {
            "seq": self.event_seq,
            "time": self.wall(),
            "action": action,
            "request_id": getattr(entry_or_grant, "id", None),
            "client": getattr(entry_or_grant, "client", None),
            "priority": getattr(entry_or_grant, "priority", None),
            **fields,
        }
        self.events.append(item)
        return item

    def events_after(self, after=None, limit=200):
        limit = max(1, min(limit, EVENT_LIMIT))
        items = [e for e in self.events if after is None or e["seq"] > after]
        return {
            "boot_id": self.boot_id,
            "next": self.event_seq,
            "oldest": self.events[0]["seq"] if self.events else None,
            "events": items[:limit],
        }

    @staticmethod
    def clean_reason(reason):
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("A reason is required")
        return reason.strip()[:MAX_REASON]

    def abort(self, request_id, reason, actor):
        """End a running generation (D12). The proxy delivers the error to the worker."""
        reason = self.clean_reason(reason)
        grant = self.running.get(request_id)
        if grant is None or grant.aborted:
            return None
        grant.abort_reason = reason
        grant.abort_event.set()
        self.aborted += 1
        return self.emit("aborted_by_operator", grant, reason=reason, actor=actor)

    def drop(self, request_id, reason, actor):
        """Remove a waiting request; its client gets 429 + Retry-After."""
        reason = self.clean_reason(reason)
        entry = next((e for e in self.queue if e.id == request_id), None)
        if entry is None or entry.future.done():
            return None
        entry.dropped = (reason, self.retry_after())
        self.counters[entry.priority]["dropped"] += 1
        event = self.emit("dropped_by_operator", entry, reason=reason, actor=actor)
        self.dispatch()
        return event

    def set_paused(self, paused, actor):
        self.store.data["paused"] = bool(paused)
        self.store.save()
        event = self.emit("batch_paused" if paused else "batch_resumed", actor=actor)
        self.dispatch()
        return event

    def set_override(self, value, actor):
        """``value`` 0 or more reserves that many slots until local midnight; None clears it."""
        if value is None:
            self.store.data["override"] = None
        else:
            _int(value, "reservation override", 0, 64)
            local = self.now().astimezone()
            midnight = (local + datetime.timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            self.store.data["override"] = {"value": value, "until": midnight.timestamp()}
        self.store.save()
        event = self.emit("reservation_override", actor=actor, value=value)
        self.dispatch()
        return event

    def set_settings(self, model_key, block, actor):
        """Replace the saved overrides for one model; ``None`` or ``{}`` returns to config."""
        if block:
            validate_settings(block, partial=True)
            self.store.data["settings"][model_key] = block
        else:
            self.store.data["settings"].pop(model_key, None)
        self.store.save()
        event = self.emit("settings_changed", actor=actor, model=model_key, settings=block or None)
        return event

    # ---- views ---------------------------------------------------------------------------

    @staticmethod
    def percentile(values, q):
        if not values:
            return None
        ordered = sorted(values)
        return ordered[min(len(ordered) - 1, int(q * len(ordered)))]

    def view(self, tap_live=None):
        now = self.clock()
        reserved = self.reserved_now()
        override = self.store.data["override"]
        running = []
        for grant in self.running.values():
            live = tap_live(grant) if tap_live else None
            running.append(
                {
                    "id": grant.id,
                    "client": grant.client,
                    "priority": grant.priority,
                    "borrowed": grant.borrowed,
                    "elapsed_seconds": round(now - grant.started, 1),
                    "waited_ms": grant.waited_ms,
                    "expected_tokens": grant.expected_tokens,
                    "expected_source": grant.expected_source,
                    "tokens": live.get("tokens") if live else None,
                    "aborting": grant.aborted,
                }
            )
        queued = [
            {
                "id": e.id,
                "client": e.client,
                "priority": e.priority,
                "waited_seconds": round(now - e.enqueued, 1),
                "expected_tokens": e.expected_tokens,
                "expected_source": e.expected_source,
                "short": e.short,
            }
            for e in self.ordered()
        ]
        return {
            "mode": "single" if self.strict() else "queued",
            "configured_mode": self.policy["mode"],
            "slots_total": self.capacity.total,
            "reserved_now": reserved,
            "reservation_override": (
                {"value": override["value"], "until": override["until"]}
                if override and override["until"] > self.wall()
                else None
            ),
            "paused_batch": self.store.data["paused"],
            "settings": self.policy,
            "running": running,
            "queue": queued,
            "counters": self.counters,
            "aborted": self.aborted,
            "wait_ms": {
                p: {
                    "last": self.waits[p][-1] if self.waits[p] else None,
                    "p95": self.percentile(self.waits[p], 0.95),
                }
                for p in PRIORITIES
            },
            "boot_id": self.boot_id,
            "event_cursor": self.event_seq,
            "state_error": self.store.error,
        }
