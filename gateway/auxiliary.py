"""Explicit auxiliary worker lifecycle, isolated from the chat inference path."""

import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import HTTPException

from .core import finish_cleanup
from .memory_admission import MemoryAdmission, MemoryAdmissionError, effective_available_memory


@dataclass
class Service:
    name: str
    models: dict
    enabled: bool = False
    model: str | None = None
    process: object = None
    busy: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class Auxiliary:
    def __init__(self, manager, options):
        self.manager = manager
        self.options = options
        self.services = {
            name: Service(name, options.get(name, {}).get("models", {}))
            for name in ("transcription", "embeddings")
        }
        self.memory = MemoryAdmission(
            effective_available_memory, options.get("minimum_free_bytes", 4 * 1024**3)
        )
        manager.auxiliary = self

    def active(self):
        return any(s.process is not None for s in self.services.values())

    def get(self, name):
        if name not in self.services:
            raise HTTPException(404, "Unknown service")
        return self.services[name]

    def chat_reserve(self):
        if self.manager.phase in ("ready", "loading"):
            n = self.options.get("chat_growth_bytes")
            if type(n) is not int or n <= 0:
                raise HTTPException(
                    409, "Configure chat memory growth budget before concurrent use"
                )
            return n
        if self.manager.native_processes():
            raise HTTPException(409, "Unmanaged native engine present; memory budget unknown")
        return 0

    def view(self):
        result = {}
        for name, service in self.services.items():
            if service.process is not None and service.process.returncode is not None:
                service.process = None
                service.model = None
            result[name] = {
                "enabled": service.enabled,
                "model": service.model,
                "loaded": service.process is not None,
                "busy": service.busy,
                "models": list(service.models),
            }
        return result

    async def enable(self, name, enabled):
        service = self.get(name)
        async with service.lock:
            if not enabled and service.busy:
                raise HTTPException(409, "Service is processing a request")
            service.enabled = enabled
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
            spec = service.models[model]
            async with self.manager.lock:
                if hasattr(self.manager, "check_update"):
                    self.manager.check_update()
                self.manager.check_lease()
                extra = self.chat_reserve()
                try:
                    async with self.memory.reserve(spec["load_bytes"] + extra):
                        env = os.environ.copy()
                        for key in ("IGM_CONTROL_KEY", "IGM_INFERENCE_KEY"):
                            env.pop(key, None)
                        env.update(
                            IGM_AUX_THREADS=str(spec.get("threads", 4)),
                            IGM_AUX_MEMORY_LIMIT=str(
                                min(spec["load_bytes"], spec["request_bytes"])
                            ),
                            HF_HUB_OFFLINE="1",
                        )
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
                        service.process = process
                        try:
                            line = await asyncio.wait_for(process.stdout.readline(), timeout=180)
                            if not line or json.loads(line).get("ready") is not True:
                                raise ValueError("worker did not become ready")
                        except BaseException:
                            await finish_cleanup(asyncio.create_task(self._stop(service)))
                            raise
                        service.model = model
                except MemoryAdmissionError as exc:
                    raise HTTPException(503, str(exc)) from None
                except (OSError, ValueError, asyncio.TimeoutError):
                    raise HTTPException(503, "Auxiliary worker failed to start") from None
        return self.view()

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

    async def unload(self, name):
        service = self.get(name)
        async with service.lock:
            if service.busy:
                raise HTTPException(409, "Service is processing a request")
            await self._stop(service)
        return self.view()

    async def infer(self, name, model, payload):
        if hasattr(self.manager, "check_update"):
            self.manager.check_update()
        service = self.get(name)
        async with service.lock:
            if not service.enabled or service.process is None or service.model != model:
                raise HTTPException(409, "Requested service model is not enabled and loaded")
            if service.busy:
                raise HTTPException(429, "Service is busy", headers={"Retry-After": "1"})
            if service.process.returncode is not None:
                service.process = None
                service.model = None
                raise HTTPException(503, "Service worker exited")
            service.busy = True
            process = service.process
            budget = service.models[model]["request_bytes"]
        try:
            extra = self.chat_reserve()
            try:
                async with self.memory.reserve(budget + extra):
                    try:
                        process.stdin.write(json.dumps(payload).encode() + b"\n")
                        await process.stdin.drain()
                        line = await asyncio.wait_for(process.stdout.readline(), timeout=3600)
                    except BaseException:
                        async with service.lock:
                            await finish_cleanup(asyncio.create_task(self._stop(service)))
                        raise
                    if not line:
                        raise RuntimeError("worker exited")
                    response = json.loads(line)
                    if "error" in response:
                        raise HTTPException(400, response["error"])
                    return response["result"]
            except MemoryAdmissionError as exc:
                raise HTTPException(503, str(exc)) from None
            except (OSError, ValueError, KeyError, RuntimeError, asyncio.TimeoutError):
                async with service.lock:
                    await self._stop(service)
                raise HTTPException(503, "Auxiliary worker failed") from None
        except asyncio.CancelledError:
            async with service.lock:
                await finish_cleanup(asyncio.create_task(self._stop(service)))
            raise
        finally:
            service.busy = False

    async def close(self):
        for service in self.services.values():
            async with service.lock:
                await self._stop(service)
