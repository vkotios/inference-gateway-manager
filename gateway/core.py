"""Single-engine lifecycle with kernel PID identity guards (Linux only)."""

import asyncio
import os
import secrets
import signal
import subprocess
import time
from pathlib import Path

import httpx
import psutil
from fastapi import HTTPException


def identity(pid):
    try:
        tail = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return None if tail[0] == "Z" else (pid, int(tail[19]))
    except (OSError, ValueError, IndexError):
        return None


def signal_identity(item, sig):
    try:
        fd = os.pidfd_open(item[0])
    except ProcessLookupError:
        return
    try:
        # Identity is checked AFTER opening the handle. A later PID reuse cannot
        # redirect the signal: the kernel handle refers to the original task.
        if identity(item[0]) == tuple(item):
            try:
                signal.pidfd_send_signal(fd, sig)
            except ProcessLookupError:
                pass
    finally:
        os.close(fd)


async def finish_cleanup(task):
    """Do not release lifecycle ownership while shielded teardown is running."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


class Manager:
    def __init__(self, config, state_dir):
        # /proc identities and pidfds must use the same numeric PID namespace.
        # Check before creating state, clients, or starting any backend process.
        try:
            proc_pid = int(Path("/proc/self/stat").read_text().split()[0])
        except (OSError, ValueError, IndexError) as exc:
            raise RuntimeError("Cannot verify /proc PID namespace; startup refused") from exc
        if proc_pid != os.getpid():
            raise RuntimeError("/proc PID namespace differs from manager; startup refused")
        self.config = config
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.engine = None
        self.model = None
        self.phase = "unloaded"
        self.process = None
        self.lock = asyncio.Lock()
        self.active = {}
        self.last_error = None
        self.owned = set()
        self.loading_task = None
        self.load_interrupted = False
        self.launch_id = None
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(1800, connect=5), trust_env=False)

    @property
    def lifecycle_busy(self):
        return self.lock.locked() or (
            self.loading_task is not None and not self.loading_task.done()
        )

    def native_processes(self):
        found = []
        for p in psutil.process_iter(["pid", "exe", "cmdline"]):
            try:
                exe = Path(p.info["exe"] or "").name
                argv = "\0".join(p.info["cmdline"] or [])
                for matcher in self.config.get("native_matchers", []):
                    if (not matcher.get("exe") or exe == matcher["exe"]) and (
                        not matcher.get("argv_contains") or matcher["argv_contains"] in argv
                    ):
                        item = identity(p.pid)
                        if item and p.pid != os.getpid():
                            found.append(item)
                        break
            except (psutil.Error, OSError):
                continue
        return found

    def capture_owned(self):
        if self.process is not None:
            item = identity(self.process.pid) if self.process.poll() is None else None
            if item:
                self.owned.add(item)
                try:
                    self.owned.update(
                        x
                        for p in psutil.Process(item[0]).children(recursive=True)
                        if (x := identity(p.pid))
                    )
                except psutil.Error:
                    pass
            # Session membership preserves ownership after ordinary reparenting.
            # Container exec may detach from both ancestry and session: its argv
            # must explicitly forward our unguessable per-launch environment tag.
            if self.launch_id:
                for candidate in self.native_processes():
                    try:
                        env = psutil.Process(candidate[0]).environ()
                        if (
                            env.get("IGM_ENGINE_RUN_ID") == self.launch_id
                            and identity(candidate[0]) == candidate
                        ):
                            self.owned.add(candidate)
                    except psutil.Error:
                        continue
            # An exited launcher's numeric SID is not ownership evidence once
            # no verified member survives: that PID/SID could have been reused.
            trusted_session = False
            for owned in tuple(self.owned):
                try:
                    if identity(owned[0]) == owned and os.getsid(owned[0]) == self.process.pid:
                        trusted_session = True
                        break
                except OSError:
                    continue
            if trusted_session:
                for p in psutil.process_iter(["pid"]):
                    try:
                        candidate = identity(p.pid)
                        if candidate and os.getsid(p.pid) == self.process.pid:
                            if identity(p.pid) == candidate:
                                self.owned.add(candidate)
                    except (psutil.Error, OSError):
                        continue

    def spec(self):
        return self.config["engines"][self.engine]["models"][self.model]

    async def native_busy(self):
        if not self.model or self.phase == "unloaded":
            return False
        spec = self.spec()
        if spec.get("probe", "slots") != "slots":
            return None
        try:
            r = await self.http.get(spec["base_url"] + "/slots", timeout=2)
            r.raise_for_status()
            slots = r.json()
            if (
                not isinstance(slots, list)
                or not slots
                or any(not isinstance(s.get("is_processing"), bool) for s in slots)
            ):
                return None
            return any(s["is_processing"] for s in slots)
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            return None

    def refresh_liveness(self):
        if self.phase == "ready" and (self.process is None or self.process.poll() is not None):
            self.phase = "error"
            self.last_error = "Backend exited; reload using control API"

    def reset_loaded(self):
        self.process = None
        self.owned.clear()
        self.model = None
        self.launch_id = None
        self.last_error = None
        self.phase = "selected" if self.engine else "unloaded"

    async def clear_requests(self):
        tasks = {entry.get("task") for entry in self.active.values()}
        self.active.clear()
        tasks.discard(None)
        tasks.discard(asyncio.current_task())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _stop(self, force=False):
        task = asyncio.create_task(self._stop_impl(force))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await finish_cleanup(task)
            raise

    def signal_owned(self, snapshot, sig):
        failed = False
        for item in snapshot:
            try:
                signal_identity(item, sig)
            except OSError:
                failed = True
        if failed:
            self.phase = "error"
            self.last_error = "Cannot signal owned backend; check process permissions"
            raise HTTPException(503, self.last_error)

    async def _stop_impl(self, force=False):
        self.capture_owned()
        alive = any(identity(p[0]) == p for p in self.owned)
        if not alive:
            await self.clear_requests()
            self.reset_loaded()
            return
        busy = await self.native_busy()
        gateway_only = self.model and self.spec().get("probe") == "gateway_only"
        if not force and (self.active or (busy is not False and not gateway_only)):
            raise HTTPException(
                409, "Engine busy or native occupancy unknown; retry or explicitly force"
            )
        self.phase = "stopping"
        if force:
            await self.clear_requests()
        self.capture_owned()
        snapshot = set(self.owned)
        self.signal_owned(snapshot, signal.SIGTERM)
        deadline = time.monotonic() + self.config.get("stop_timeout", 8)
        while any(identity(p[0]) == p for p in snapshot) and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        self.signal_owned(snapshot, signal.SIGKILL)
        deadline = time.monotonic() + 5
        while any(identity(p[0]) == p for p in snapshot) and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        if self.process is not None:
            self.process.poll()
        survivors = [p for p in snapshot if identity(p[0]) == p]
        if survivors or self.native_processes():
            self.phase = "error"
            raise HTTPException(503, "Native process survived shutdown; new engine refused")
        self.reset_loaded()

    async def interrupt_loading(self, force):
        task = self.loading_task
        if force and self.phase == "loading" and task and task is not asyncio.current_task():
            self.load_interrupted = True
            self.phase = "stopping"
            task.cancel()
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                try:
                    await finish_cleanup(task)
                except asyncio.CancelledError:
                    pass

    async def select_engine(self, engine, force=False):
        await self.interrupt_loading(force)
        async with self.lock:
            if engine not in self.config["engines"]:
                raise HTTPException(404, "Unknown engine")
            if engine == self.engine:
                return
            await self._stop(force)
            if self.native_processes():
                raise HTTPException(409, "Unmanaged native engines present; reconcile first")
            self.engine = engine
            self.model = None
            self.phase = "selected"

    async def unload(self, force=False):
        await self.interrupt_loading(force)
        async with self.lock:
            await self._stop(force)

    async def load_model(self, model, force=False):
        await self.interrupt_loading(force)
        if self.lifecycle_busy:
            raise HTTPException(409, "Lifecycle operation already running")
        task = asyncio.create_task(self._load_model(model, force))
        self.loading_task = task
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise HTTPException(409, "Load cancelled by force") from None
            task.cancel()
            try:
                await finish_cleanup(task)
            except asyncio.CancelledError:
                pass
            raise
        finally:
            if self.loading_task is task:
                self.loading_task = None

    async def _load_model(self, model, force=False):
        async with self.lock:
            if not self.engine:
                raise HTTPException(409, "Select an engine before a model")
            models = self.config["engines"][self.engine]["models"]
            if model not in models or not models[model].get("enabled", True):
                raise HTTPException(404, "Unknown or disabled model for selected engine")
            if model == self.model and self.phase == "ready" and self.process.poll() is None:
                return
            await self._stop(force)
            if self.native_processes():
                raise HTTPException(409, "Native engines still running; load refused")
            spec = models[model]
            self.model = model
            self.phase = "loading"
            self.loading_task = asyncio.current_task()
            self.load_interrupted = False
            self.last_error = None
            self.launch_id = secrets.token_hex(24)
            try:
                with (self.state_dir / "engine.log").open("ab") as log:
                    self.process = subprocess.Popen(
                        [arg.replace("{launch_id}", self.launch_id) for arg in spec["argv"]],
                        cwd=spec.get("cwd"),
                        env={
                            **{
                                k: v
                                for k, v in {**os.environ, **spec.get("env", {})}.items()
                                if not k.startswith("IGM_")
                                and k not in {"NOTIFY_SOCKET", "WATCHDOG_USEC", "WATCHDOG_PID"}
                            },
                            "IGM_ENGINE_RUN_ID": self.launch_id,
                        },
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=log,
                        start_new_session=True,
                    )
                self.capture_owned()
                deadline = time.monotonic() + self.config.get("startup_timeout", 900)
                while time.monotonic() < deadline:
                    if self.process.poll() is not None:
                        raise RuntimeError("Backend exited while loading; inspect local engine log")
                    self.capture_owned()
                    if any(p not in self.owned for p in self.native_processes()):
                        raise RuntimeError("Unmanaged native engine appeared during startup")
                    try:
                        r = await self.http.get(
                            spec["base_url"] + spec.get("ready_path", "/health"), timeout=2
                        )
                        if r.is_success:
                            self.phase = "ready"
                            return
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(0.25)
                raise TimeoutError("Model startup timed out")
            except BaseException:
                await finish_cleanup(asyncio.create_task(self._stop(force=True)))
                if self.load_interrupted:
                    self.last_error = None
                else:
                    self.last_error = "Backend startup failed; inspect local engine log"
                    self.phase = "error"
                raise
            finally:
                self.loading_task = None

    async def close(self):
        try:
            await self.unload(force=True)
        finally:
            await self.http.aclose()
