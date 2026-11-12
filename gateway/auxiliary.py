"""Explicit auxiliary worker lifecycle, isolated from the chat inference path.

Memory is managed with recorded footprints (see ``footprints``): whether a combination of
the loaded chat model and auxiliary workers fits is computed from recorded numbers, never
from per-family constants. The main chat model always has priority: a chat load unloads
only the auxiliary services that no longer fit (all of them when the chat model has no
recorded footprint), and never waits on or is refused by them.

Lock order is ``manager.lock`` then ``service.lock``. The chat path therefore never takes a
service lock; it flags a service as draining and stops it directly.
"""

import asyncio
import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import HTTPException

from .core import finish_cleanup
from .footprint_recorder import process_resident_bytes
from .footprints import (
    CHAT,
    Footprint,
    FootprintStore,
    aux_fingerprint,
    chat_fingerprint,
    fit,
)
from .memory_admission import (
    MemoryAdmission,
    MemoryAdmissionError,
    effective_available_memory,
    effective_total_memory,
)

GIB = 1024**3
RESTORE_POLICIES = ("never", "after_clean_shutdown", "on_demand")
DEFAULT_DRAIN = {"embeddings": 30, "transcription": 600}
MAX_FAILED_RESTORES_PER_HOUR = 3
MAX_HOLDOFF_SECONDS = 7 * 24 * 3600
STATE_VERSION = 1


def refusal(status, message, code, retry=None):
    headers = {"X-IGM-Code": code}
    if retry is not None:
        headers["Retry-After"] = str(retry)
    return HTTPException(status, message, headers=headers)


def disk_size(path):
    """Bytes under a model path (file or directory); unreadable entries count as zero."""
    path = Path(path)
    try:
        if path.is_file():
            return path.stat().st_size
        total = 0
        for item in path.rglob("*"):
            try:
                if item.is_file():
                    total += item.stat().st_size
            except OSError:
                continue
        return total
    except OSError:
        return 0


def worker_hwm_bytes(pid, proc="/proc"):
    try:
        for line in (Path(proc) / str(pid) / "status").read_text().splitlines():
            if line.startswith("VmHWM:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


@dataclass
class Service:
    name: str
    models: dict
    enabled: bool = False
    model: str | None = None  # loaded now
    desired_model: str | None = None  # what the operator last asked to have loaded
    loading_model: str | None = None
    process: object = None
    busy: bool = False
    draining: bool = False
    loading_task: object = None
    suspended_reason: str | None = None
    resume_after: float | None = None
    steady: int = 0
    hwm: int = 0
    requests_seen: int = 0
    last_error: str | None = None
    previous: dict | None = None  # remembered desired state that was not restored
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class Auxiliary:
    def __init__(
        self,
        manager,
        options,
        store=None,
        *,
        clock=time.monotonic,
        wall=time.time,
        sample_total=effective_total_memory,
    ):
        self.manager = manager
        self.options = options
        self.clock = clock
        self.wall = wall
        self.sample_total = sample_total
        self.store = store if store is not None else FootprintStore(None)
        self.services = {
            name: Service(name, options.get(name, {}).get("models", {}))
            for name in ("transcription", "embeddings")
        }
        self.memory = MemoryAdmission(
            effective_available_memory, options.get("minimum_free_bytes", 4 * GIB)
        )
        self.policy = options.get("restore", "never")
        self.holdoff = options.get("restore_holdoff_seconds", 3600)
        self.state_path = Path(manager.state_dir) / "auxiliary-state.json"
        self.blocked_reason = None
        self.attempts = []  # wall-clock times of failed automatic restores
        self._override = {}  # restore settings saved from the portal; they win over the config
        self._fingerprints = {}
        self._probation = {}
        self._wake = asyncio.Event()
        self._demand = set()
        self._task = None
        self._load_state()
        manager.auxiliary = self

    # -- configuration -----------------------------------------------------------------

    @property
    def baseline_bytes(self):
        return self.options.get("host_baseline_bytes", 6 * GIB)

    @property
    def reserve_bytes(self):
        return self.memory.minimum_free_bytes

    def chat_growth(self):
        """Optional extra live headroom reserved for the chat model while it is loaded."""
        if self.manager.phase in ("ready", "loading"):
            return self.options.get("chat_growth_bytes", 0)
        return 0

    def drain_seconds(self, kind):
        configured = self.options.get("drain_seconds", {})
        if isinstance(configured, int):
            return configured
        return configured.get(kind, DEFAULT_DRAIN[kind])

    def active(self):
        return any(s.process is not None for s in self.services.values())

    def get(self, name):
        if name not in self.services:
            raise HTTPException(404, "Unknown service")
        return self.services[name]

    # -- persisted state ---------------------------------------------------------------

    def _load_state(self):
        data = {}
        try:
            data = json.loads(self.state_path.read_text())
            if data.get("version") != STATE_VERSION:
                data = {}
        except (OSError, ValueError, AttributeError):
            data = {}
        saved_settings = data.get("settings") if isinstance(data.get("settings"), dict) else {}
        if saved_settings.get("restore") in RESTORE_POLICIES:
            self.policy = self._override["restore"] = saved_settings["restore"]
        holdoff = saved_settings.get("restore_holdoff_seconds")
        if type(holdoff) is int and 0 <= holdoff <= MAX_HOLDOFF_SECONDS:
            self.holdoff = self._override["restore_holdoff_seconds"] = holdoff
        was_clean = bool(data.get("clean_shutdown", False))
        in_progress = data.get("restore_in_progress")
        self.attempts = [t for t in data.get("failed_restores", []) if isinstance(t, (int, float))]
        saved = data.get("services", {}) if isinstance(data.get("services"), dict) else {}
        restorable = self.policy != "never" and was_clean and not in_progress
        if self.policy != "never" and not restorable:
            if in_progress:
                self.blocked_reason = "A previous automatic restore did not complete"
            elif data:
                self.blocked_reason = "Previous shutdown was not clean"
        for name, service in self.services.items():
            entry = saved.get(name) if isinstance(saved.get(name), dict) else {}
            desired = entry.get("desired_model")
            enabled = bool(entry.get("enabled"))
            if desired not in service.models:
                desired = None
            if restorable:
                service.enabled, service.desired_model = enabled, desired
            elif enabled or desired:
                service.previous = {"enabled": enabled, "desired_model": desired}
        # A crash from here on must be distinguishable from a clean stop.
        self._state = {
            "clean_shutdown": False,
            "restore_in_progress": None,
        }
        self._save_state()

    def _save_state(self, **changes):
        self._state.update(changes)
        payload = {
            "version": STATE_VERSION,
            "clean_shutdown": self._state["clean_shutdown"],
            "restore_in_progress": self._state["restore_in_progress"],
            "failed_restores": self.attempts[-10:],
            "settings": dict(self._override),
            "services": {
                name: {"enabled": s.enabled, "desired_model": s.desired_model}
                for name, s in self.services.items()
            },
        }
        try:
            fd, name = tempfile.mkstemp(prefix=".aux-state-", dir=self.state_path.parent)
            try:
                with os.fdopen(fd, "w") as out:
                    json.dump(payload, out, allow_nan=False)
                    out.flush()
                    os.fsync(out.fileno())
                os.chmod(name, 0o600)
                os.replace(name, self.state_path)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
        except OSError:
            # Losing persistence must never break serving; restore just stays manual.
            pass

    # -- memory model ------------------------------------------------------------------

    def aux_fp(self, name, model, fresh=False):
        """Launch fingerprint of an auxiliary model; the file walk is cached for 30 s."""
        key, now = (name, model), self.clock()
        cached = self._fingerprints.get(key)
        if cached is not None and not fresh and now - cached[1] < 30:
            return cached[0]
        value = aux_fingerprint(name, model, self.get(name).models[model])
        self._fingerprints[key] = (value, now)
        return value

    def _probation_bytes(self, name, model, spec):
        """Conservative first-load budget: four times the model's size on disk (min 1 GiB)."""
        key, now = (name, model), self.clock()
        cached = self._probation.get(key)
        if cached is None or now - cached[1] >= 60:
            cached = (max(GIB, 4 * disk_size(spec["path"])), now)
            self._probation[key] = cached
        return cached[0]

    def aux_footprint(self, name, model):
        """Recorded footprint, else the administrator's budget, else a conservative probation."""
        spec = self.get(name).models[model]
        found = self.store.lookup(name, model, self.aux_fp(name, model))
        if found is not None:
            return found
        if "load_bytes" in spec:
            request = spec.get("request_bytes", spec["load_bytes"])
            return Footprint(
                name,
                model,
                spec["load_bytes"],
                spec["load_bytes"] + request,
                request,
                None,
                None,
                "estimate",
                0.0,
                "configured budget",
                1,
            )
        probation = self._probation_bytes(name, model, spec)
        return Footprint(
            name,
            model,
            probation,
            2 * probation,
            probation,
            None,
            None,
            "estimate",
            0.0,
            "probation: not measured yet",
            1,
        )

    def chat_entries(self):
        """Ledger entry for the loaded chat model; unrecorded models map to ``None``."""
        m = self.manager
        if m.phase == "ready" and m.model:
            key = f"{m.engine}/{m.model}"
            context = (m.active_settings or {}).get("context")
            record = self.store.lookup(
                CHAT, key, chat_fingerprint(m.engine, m.model, m.active_spec or {}), context
            )
            return [(CHAT, key, record)]
        if m.native_processes():
            raise refusal(
                409, "Unmanaged native engine present; memory budget unknown", "unmanaged_engine"
            )
        return []

    def _live_services(self, exclude=None):
        return [
            s
            for s in self.services.values()
            if s is not exclude
            and (
                s.process is not None or (s.loading_task is not None and not s.loading_task.done())
            )
        ]

    def _aux_entry(self, service, model=None):
        model = model or service.model or service.loading_model or service.desired_model
        if model not in service.models:
            return (service.name, str(model), None)  # unknown model: cannot be verified
        return (service.name, model, self.aux_footprint(service.name, model))

    def live_services(self):
        return self._live_services()

    def aux_entry(self, service):
        return self._aux_entry(service)

    def total_memory(self):
        return self._total()

    def _total(self):
        try:
            total = self.sample_total()
        except (OSError, ValueError, RuntimeError):
            return None
        return total if type(total) is int and total > 0 else None

    def fit_decision(self, chat_entries, aux_entries):
        total = self._total()
        if total is None:
            return None
        return fit(total, self.reserve_bytes, self.baseline_bytes, [*chat_entries, *aux_entries])

    def fit_with_chat(self, engine, model, spec, settings, services):
        """Would this chat model fit next to these services?  ``None`` if memory is unknown."""
        key = f"{engine}/{model}"
        record = self.store.lookup(
            CHAT, key, chat_fingerprint(engine, model, spec), settings.get("context")
        )
        return self.fit_decision([(CHAT, key, record)], [self._aux_entry(s) for s in services])

    # -- chat priority -----------------------------------------------------------------

    async def make_room_for_chat(self, engine, model, spec, settings):
        """Unload the auxiliary services that do not fit with this chat model.

        Called with the lifecycle lock held. Nothing is unloaded when everything fits;
        a chat model with no recorded footprint cannot be verified against any service.
        """
        live = self._live_services()
        if not live:
            return
        key = f"{engine}/{model}"
        record = self.store.lookup(
            CHAT, key, chat_fingerprint(engine, model, spec), settings.get("context")
        )
        if record is None:
            reason, evict = "chat_uncalibrated", list(live)
        else:
            reason, evict = "chat_priority", []
            remaining = sorted(
                live, key=lambda s: -(getattr(self._aux_entry(s)[2], "needs_bytes", 0))
            )
            while remaining:
                decision = self.fit_decision(
                    [(CHAT, key, record)], [self._aux_entry(s) for s in remaining]
                )
                if decision is not None and decision.fits:
                    break
                evict.append(remaining.pop(0))
        for service in evict:
            await self.suspend(service, reason)

    async def suspend(self, service, reason):
        """Unload for the chat model, keeping the operator's desired state.

        New requests are refused immediately; in-flight work gets a bounded drain.
        """
        service.draining = True
        service.suspended_reason = reason
        service.resume_after = self.clock() + self.holdoff
        try:
            task = service.loading_task
            if task is not None and not task.done():
                task.cancel()
                try:
                    await finish_cleanup(task)
                except (asyncio.CancelledError, HTTPException):
                    pass
            deadline = self.clock() + self.drain_seconds(service.name)
            while service.busy and self.clock() < deadline:
                await asyncio.sleep(0.05)
            await self._stop(service)
        finally:
            service.draining = False

    # -- operator controls -------------------------------------------------------------

    def _models_view(self, service):
        view = {}
        for model in service.models:
            fp = self.aux_footprint(service.name, model)
            view[model] = {
                "source": fp.source,
                "calibrated": fp.source == "measured",
                "needs_bytes": fp.needs_bytes,
                "note": fp.note,
            }
        return view

    def view(self):
        result = {}
        for name, service in self.services.items():
            if service.process is not None and service.process.returncode is not None:
                service.process = None
                service.model = None
            suspended = service.suspended_reason
            result[name] = {
                "enabled": service.enabled,
                "model": service.model,
                "loaded": service.process is not None,
                "busy": service.busy,
                "models": list(service.models),
                "desired_model": service.desired_model,
                "suspended_reason": suspended,
                "resume_in_seconds": (
                    max(0, round(service.resume_after - self.clock()))
                    if suspended and service.resume_after is not None
                    else None
                ),
                "last_error": service.last_error,
                "footprints": self._models_view(service),
                "restore": {
                    "policy": self.policy,
                    "holdoff_seconds": self.holdoff,
                    "blocked": self.blocked_reason,
                },
                "previous": service.previous,
            }
        return result

    def settings_view(self):
        return {
            "restore": self.policy,
            "restore_holdoff_seconds": self.holdoff,
            "configured_restore": self.options.get("restore", "never"),
            "configured_restore_holdoff_seconds": self.options.get("restore_holdoff_seconds", 3600),
            "overridden": bool(self._override),
            "blocked": self.blocked_reason,
        }

    async def set_settings(self, restore=None, holdoff=None):
        """Change the restore policy or hold-off at runtime; saved, and applied immediately."""
        if restore is None and holdoff is None:
            raise HTTPException(422, "Nothing to change")
        if restore is not None and restore not in RESTORE_POLICIES:
            raise HTTPException(422, "Unknown restore policy")
        if holdoff is not None and (
            type(holdoff) is not int or not 0 <= holdoff <= MAX_HOLDOFF_SECONDS
        ):
            raise HTTPException(422, "Restore delay must be between 0 and 7 days")
        if restore is not None:
            self.policy = self._override["restore"] = restore
        if holdoff is not None:
            self.holdoff = self._override["restore_holdoff_seconds"] = holdoff
        self._save_state()
        if self.policy == "never":
            await self._stop_loop()
        else:
            self.start()
            self.kick()
        return self.settings_view()

    async def enable(self, name, enabled):
        service = self.get(name)
        async with service.lock:
            if not enabled and service.busy:
                raise HTTPException(409, "Service is processing a request")
            service.enabled = enabled
            if not enabled:
                service.desired_model = None
                service.suspended_reason = None
            self._save_state()
        return self.view()

    async def load(self, name, model):
        service = self.get(name)
        async with service.lock:
            if not service.enabled:
                raise HTTPException(409, "Enable service before loading")
            if service.busy:
                raise HTTPException(409, "Service is busy")
            if model not in service.models:
                raise HTTPException(404, "Unknown auxiliary model")
            if service.process and service.process.returncode is None:
                if service.model == model:
                    return self.view()
                raise HTTPException(409, "Unload the current service model first")
            await self._load_locked(service, model)
            # An explicit operator action sets the desired state and clears any restore block.
            service.desired_model = model
            service.suspended_reason = None
            self.blocked_reason = None
            self._save_state()
        return self.view()

    async def unload(self, name):
        service = self.get(name)
        async with service.lock:
            if service.busy:
                raise HTTPException(409, "Service is processing a request")
            await self._stop(service)
            service.desired_model = None
            service.suspended_reason = None
            self._save_state()
        return self.view()

    async def _load_locked(self, service, model):
        """Start the worker as a task so a chat load can cancel it. Caller holds service.lock."""
        task = asyncio.create_task(self._start(service, model))
        service.loading_task = task
        service.loading_model = model
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise refusal(
                    409, "Auxiliary load cancelled: a chat model is loading", "chat_priority"
                ) from None
            task.cancel()
            try:
                await finish_cleanup(task)
            except asyncio.CancelledError:
                pass
            raise
        finally:
            if service.loading_task is task:
                service.loading_task = None
                service.loading_model = None

    async def _start(self, service, model):
        name = service.name
        spec = service.models[model]
        self.aux_fp(name, model, fresh=True)  # a changed model directory is noticed at load
        footprint = self.aux_footprint(name, model)
        if self.manager.lifecycle_busy:
            raise HTTPException(409, "Lifecycle operation already running")
        # The lifecycle lock is held for the admission decision only, never while the
        # worker starts, so chat inference and chat loads are not blocked by a slow load.
        async with self.manager.lock:
            if hasattr(self.manager, "check_update"):
                self.manager.check_update()
            self.manager.check_lease()
            chat = self.chat_entries()
            others = [self._aux_entry(s) for s in self._live_services(exclude=service)]
            decision = self.fit_decision(chat, [*others, (name, model, footprint)])
            if decision is None:
                raise refusal(503, "Total memory could not be measured", "memory_unknown", 60)
            if decision.reason == "uncalibrated":
                raise refusal(
                    409,
                    "The loaded chat model has no recorded memory footprint; record it before "
                    "loading auxiliary services",
                    "model_uncalibrated",
                )
            if not decision.fits:
                raise refusal(
                    503,
                    f"Insufficient memory for this combination: needs "
                    f"{decision.needed_bytes / GIB:.1f} GiB, budget {decision.budget_bytes / GIB:.1f} GiB",
                    "insufficient_memory",
                    60,
                )
            try:
                _live, token = await self.memory.acquire(footprint.needs_bytes + self.chat_growth())
            except MemoryAdmissionError as exc:
                raise refusal(503, str(exc), "insufficient_memory", 60) from None
        try:
            env = os.environ.copy()
            for key in ("IGM_CONTROL_KEY", "IGM_INFERENCE_KEY"):
                env.pop(key, None)
            env.update(
                IGM_AUX_THREADS=str(spec.get("threads", 4)),
                IGM_AUX_MEMORY_LIMIT=str(max(8 * GIB, 4 * footprint.needs_bytes)),
                HF_HUB_OFFLINE="1",
            )
            try:
                process = await asyncio.create_subprocess_exec(
                    self.options.get("worker_python", sys.executable),
                    str(Path(__file__).with_name("aux_worker.py")),
                    name,
                    spec["path"],
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    env=env,
                    limit=16 * 1024 * 1024,
                )
            except OSError:
                raise refusal(503, "Auxiliary worker failed to start", "worker_failed") from None
            service.process = process
            try:
                line = await asyncio.wait_for(process.stdout.readline(), timeout=180)
                if not line or json.loads(line).get("ready") is not True:
                    raise ValueError("worker did not become ready")
            except asyncio.CancelledError:
                await finish_cleanup(asyncio.create_task(self._stop(service)))
                raise
            except (OSError, ValueError, asyncio.TimeoutError):
                await finish_cleanup(asyncio.create_task(self._stop(service)))
                raise refusal(503, "Auxiliary worker failed to start", "worker_failed") from None
            service.model = model
            service.last_error = None
            service.hwm = 0
            service.requests_seen = 0
            self._record_worker(service, model, spec)
        finally:
            self.memory.release(token)

    async def _stop(self, service):
        process = service.process
        if process:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.wait()
        service.process = None
        service.model = None

    # -- measurement of the workers themselves ----------------------------------------

    def resident_bytes(self):
        """Resident memory of all live workers (used to keep it out of the chat record)."""
        return sum(
            process_resident_bytes(s.process.pid)
            for s in self.services.values()
            if s.process is not None and s.process.returncode is None
        )

    def _record_worker(self, service, model, spec):
        pid = service.process.pid
        steady = process_resident_bytes(pid)
        if steady <= 0:
            return
        service.steady = steady
        service.hwm = max(worker_hwm_bytes(pid), steady)
        self._remember(
            Footprint(
                service.name,
                model,
                steady,
                service.hwm,
                0,
                None,
                self.aux_fp(service.name, model),
                "measured",
                self.wall(),
                "recorded at load",
                0,
            )
        )

    def _remember(self, footprint):
        """Persist a measurement; a failing disk must never take a working service down."""
        try:
            self.store.record(footprint)
        except (OSError, ValueError):
            pass

    def _observe_request(self, service):
        """After a request: raise the recorded peak if the worker grew (VmHWM)."""
        process, model = service.process, service.model
        if process is None or model is None or service.steady <= 0:
            return
        service.requests_seen += 1
        hwm = worker_hwm_bytes(process.pid)
        if hwm <= 0:
            return
        extra = max(0, hwm - service.steady)
        if hwm > service.hwm or service.requests_seen == 1:
            service.hwm = max(service.hwm, hwm)
            self._remember(
                Footprint(
                    service.name,
                    model,
                    service.steady,
                    service.hwm,
                    extra,
                    None,
                    self.aux_fp(service.name, model),
                    "measured",
                    self.wall(),
                    "peak observed while serving",
                    service.requests_seen,
                )
            )

    # -- inference ---------------------------------------------------------------------

    def unavailable(self, service):
        if not service.enabled:
            return HTTPException(409, "Requested service model is not enabled and loaded")
        if service.draining or service.suspended_reason:
            return refusal(
                503,
                "Service is unloaded so the chat model can use the memory",
                "auxiliary_unloaded_for_chat",
                60,
            )
        if service.desired_model and self.policy == "on_demand" and not self.blocked_reason:
            self.kick(service.name, demand=True)
            return refusal(503, "Service is restoring; retry shortly", "auxiliary_restoring", 5)
        return HTTPException(409, "Requested service model is not enabled and loaded")

    async def infer(self, name, model, payload):
        if hasattr(self.manager, "check_update"):
            self.manager.check_update()
        service = self.get(name)
        async with service.lock:
            if service.draining:
                raise self.unavailable(service)
            if not service.enabled or service.process is None or service.model != model:
                raise self.unavailable(service)
            if service.busy:
                raise HTTPException(429, "Service is busy", headers={"Retry-After": "1"})
            if service.process.returncode is not None:
                service.process = None
                service.model = None
                raise HTTPException(503, "Service worker exited")
            service.busy = True
            process = service.process
            footprint = self.aux_footprint(name, model)
            budget = footprint.request_estimate_bytes
        try:
            try:
                async with self.memory.reserve(budget + self.chat_growth()):
                    try:
                        process.stdin.write(json.dumps(payload).encode() + b"\n")
                        await process.stdin.drain()
                        line = await asyncio.wait_for(process.stdout.readline(), timeout=3600)
                    except BaseException:
                        await self._fail(service)
                        raise
                    if not line:
                        raise RuntimeError("worker exited")
                    response = json.loads(line)
                    if "error" in response:
                        raise HTTPException(400, response["error"])
                    self._observe_request(service)
                    return response["result"]
            except MemoryAdmissionError as exc:
                raise refusal(503, str(exc), "insufficient_memory", 30) from None
            except (OSError, ValueError, KeyError, RuntimeError, asyncio.TimeoutError):
                await self._fail(service, recover=True)
                if service.suspended_reason:
                    raise self.unavailable(service) from None
                raise HTTPException(503, "Auxiliary worker failed") from None
        except asyncio.CancelledError:
            await finish_cleanup(asyncio.create_task(self._fail(service, recover=True)))
            raise
        finally:
            service.busy = False

    async def _fail(self, service, recover=False):
        """Stop a worker whose request protocol can no longer be trusted."""
        async with service.lock:
            await self._stop(service)
        if recover:
            self.kick(service.name, demand=False)

    # -- automatic restore -------------------------------------------------------------

    def kick(self, name=None, demand=False):
        if demand and name:
            self._demand.add(name)
        self._wake.set()

    def start(self):
        """Begin the reconcile loop (no-op under the ``never`` policy)."""
        if self.policy != "never" and self._task is None:
            self._task = asyncio.create_task(self._reconcile_loop())
            self.kick()

    async def _reconcile_loop(self):
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            try:
                await self.reconcile_once()
            except Exception:  # keep the loop alive; the next pass retries
                continue

    def _recent_failures(self):
        horizon = self.wall() - 3600
        self.attempts = [t for t in self.attempts if t >= horizon]
        return len(self.attempts)

    async def reconcile_once(self):
        """Restore services the operator wants loaded, when the policy and memory allow it."""
        if self.policy == "never" or self.blocked_reason:
            return
        for service in self.services.values():
            wanted = service.enabled and service.desired_model
            if not wanted or service.process is not None or service.loading_task is not None:
                continue
            if self.manager.lifecycle_busy or service.lock.locked():
                continue
            demand = service.name in self._demand
            if self.policy == "on_demand" and not demand:
                continue
            if (
                service.suspended_reason
                and not demand
                and self.clock() < (service.resume_after or 0)
            ):
                continue
            if self._recent_failures() >= MAX_FAILED_RESTORES_PER_HOUR:
                self.blocked_reason = "Automatic restore suspended after repeated failures"
                return
            self._demand.discard(service.name)
            await self._auto_restore(service)

    async def _auto_restore(self, service):
        marker = {"service": service.name, "model": service.desired_model, "at": self.wall()}
        self._save_state(restore_in_progress=marker)
        try:
            async with service.lock:
                if service.process is not None:
                    return
                await self._load_locked(service, service.desired_model)
                service.suspended_reason = None
                service.last_error = None
        except HTTPException as exc:
            service.last_error = str(exc.detail)
            if exc.headers and exc.headers.get("X-IGM-Code") in (
                "chat_priority",
                "model_uncalibrated",
                "insufficient_memory",
            ):
                pass  # memory simply does not allow it yet: not a failed attempt
            else:
                self.attempts.append(self.wall())
        finally:
            self._save_state(restore_in_progress=None)

    # -- shutdown ----------------------------------------------------------------------

    async def _stop_loop(self):
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def close(self):
        await self._stop_loop()
        for service in self.services.values():
            await self._stop(service)
        self._save_state(clean_shutdown=True)
