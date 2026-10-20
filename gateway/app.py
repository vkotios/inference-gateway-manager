"""Authenticated control API. Inference and control credentials are separate."""

import asyncio
import hmac
import ipaddress
import json
import math
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, StrictInt

from .browser_auth import COOKIE, BrowserAuth, install_routes, origin_allowed, session_io
from .core import Manager, finish_cleanup
from .registry import Registry
from .settings import apply_reasoning, summary
from .supervision import watchdog
from .telemetry import telemetry


def reject_constant(value):
    raise ValueError("Nonfinite JSON number")


def finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Nonfinite JSON number")
    return number


def validate_json_depth(body):
    stack = [(body, 0)]
    while stack:
        value, depth = stack.pop()
        if depth > 128:
            raise ValueError("JSON nesting exceeds 128 levels")
        children = (
            value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
        )
        stack.extend((child, depth + 1) for child in children)


class Force(BaseModel):
    model_config = ConfigDict(extra="forbid")
    force: bool = False


class RecoveryConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmation: str


class ModelSelection(Force):
    model: str
    engine: str | None = None
    context: StrictInt | None = None
    reasoning: str | None = None


class EngineSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    engine: str
    force: bool = False


class InferenceLifetime:
    """Track the outer ASGI task, including socket drain after generator yield."""

    def __init__(self, app, manager):
        self.app = app
        self.manager = manager

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or not scope["path"].startswith("/v1/")
        ):
            return await self.app(scope, receive, send)
        request_id = uuid.uuid4().hex
        scope["igm_request_id"] = request_id
        scope["igm_response_task"] = asyncio.current_task()
        try:
            await self.app(scope, receive, send)
        finally:
            self.manager.active.pop(request_id, None)


def create_app(config, admin_key, inference_key, state_dir):
    if min(len(admin_key), len(inference_key)) < 32 or admin_key == inference_key:
        raise ValueError("Two different keys of at least 32 characters are required")
    manager = Manager(config, Path(state_dir))

    @asynccontextmanager
    async def lifespan(app):
        async def initialise():
            default = config.get("default")
            if default:
                try:
                    await manager.select_engine(default["engine"])
                    await manager.load_model(default["model"])
                except Exception:
                    manager.last_error = (
                        "Default model failed to start; inspect native processes and engine log"
                    )
                    manager.phase = "error"

        startup = asyncio.create_task(initialise())
        supervisor = asyncio.create_task(watchdog())
        try:
            yield
        finally:
            supervisor.cancel()
            await asyncio.gather(supervisor, return_exceptions=True)
            startup.cancel()
            try:
                await startup
            except asyncio.CancelledError:
                pass
            await manager.close()

    app = FastAPI(
        title="Inference Gateway Manager",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.state.manager = manager
    browser_auth = BrowserAuth(state_dir, config.get("browser_auth", {}))
    app.state.browser_auth = browser_auth
    install_routes(app, browser_auth, config)
    registry = Registry(config, manager.state_dir)
    app.state.registry = registry
    registry_lock = asyncio.Lock()
    recovery_lock = asyncio.Lock()
    recovery_pending = False

    @app.get("/api/recovery")
    async def recovery_capabilities():
        options = config.get("recovery", {})
        return {
            "restart_gateway": options.get("restart_gateway", False) is True,
            "reboot_host": options.get("reboot_host", False) is True,
            "pending": recovery_pending,
        }

    @app.post("/api/recovery/{action}", status_code=202)
    async def recovery_action(action: str, payload: RecoveryConfirmation):
        nonlocal recovery_pending
        option = {"restart-gateway": "restart_gateway", "reboot-host": "reboot_host"}.get(action)
        if option is None:
            raise HTTPException(404, "Unknown recovery action")
        if config.get("recovery", {}).get(option) is not True:
            raise HTTPException(403, "Recovery action disabled by administrator")
        if payload.confirmation != action:
            raise HTTPException(400, "Exact action confirmation required; this interrupts work")
        if recovery_lock.locked() or recovery_pending:
            raise HTTPException(409, "Recovery already pending")
        async with recovery_lock:
            try:
                process = await asyncio.create_subprocess_exec(
                    "/usr/bin/sudo",
                    "-n",
                    "/usr/local/libexec/igm-recovery",
                    action,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    env={"PATH": "/usr/bin:/usr/sbin", "LANG": "C"},
                )
                try:
                    await asyncio.wait_for(process.wait(), 10)
                except BaseException:
                    if process.returncode is None:
                        process.kill()
                        await process.wait()
                    raise
                if process.returncode != 0:
                    raise HTTPException(
                        503, "Recovery scheduling refused; check helper and OS permissions"
                    )
            except (OSError, TimeoutError):
                raise HTTPException(503, "Recovery helper unavailable or timed out") from None
            recovery_pending = True
        return {"action": action, "status": "scheduled", "delay_seconds": 3}

    @app.middleware("http")
    async def security(request: Request, call_next):
        networks = config.get("allowed_networks")
        if networks:
            try:
                client = ipaddress.ip_address(request.client.host)
                permitted = any(client in ipaddress.ip_network(n) for n in networks)
            except (ValueError, AttributeError):
                permitted = False
            if not permitted:
                return JSONResponse({"detail": "Client network not allowed"}, status_code=403)
        try:
            authority = urlsplit("//" + request.headers.get("host", ""))
            host = authority.hostname
            _ = authority.port  # Validate malformed ports, including IPv6 authorities.
            if (
                not host
                or authority.username
                or authority.path
                or authority.query
                or authority.fragment
            ):
                raise ValueError("Invalid Host")
        except ValueError:
            return JSONResponse({"detail": "Host not allowed"}, status_code=403)
        allowed = {
            h.strip("[]").lower() for h in config.get("allowed_hosts", ["localhost", "127.0.0.1"])
        }
        if host not in allowed:
            return JSONResponse({"detail": "Host not allowed"}, status_code=403)
        origin = request.headers.get("origin")
        same_origin = f"{request.url.scheme}://{request.headers.get('host')}"
        if origin and origin != same_origin and origin not in config.get("allowed_origins", []):
            return JSONResponse({"detail": "Cross-origin access denied"}, status_code=403)
        public_auth = request.url.path in {
            "/api/auth/config",
            "/api/auth/login",
            "/api/auth/session",
        }
        if not public_auth and (
            request.url.path.startswith(("/api/", "/v1/")) or request.url.path == "/openapi.json"
        ):
            auth = request.headers.get("authorization", "")
            token = (auth[7:] if auth[:7].lower() == "bearer " else "").encode("utf-8")
            is_admin = hmac.compare_digest(token, admin_key.encode("utf-8"))
            is_inference = hmac.compare_digest(token, inference_key.encode("utf-8"))
            # Supplying an invalid Bearer header must never fall back to a browser cookie.
            if not auth and request.url.path.startswith("/api/"):
                try:
                    browser_auth.check_transport(browser_auth.scheme(request))
                    session = await session_io(
                        browser_auth.authenticate, request.cookies.get(COOKIE)
                    )
                    if session:
                        if request.method not in ("GET", "HEAD", "OPTIONS"):
                            browser_auth.check_csrf(
                                session,
                                request.headers.get("x-csrf-token"),
                                origin_allowed(request, config),
                            )
                        request.state.browser_session = session
                        is_admin = True
                except HTTPException as exc:
                    # A missing cookie remains a normal bearer-auth rejection.
                    if request.cookies.get(COOKIE):
                        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
            if not (is_admin or is_inference):
                return JSONResponse({"detail": "Bearer authentication required"}, status_code=401)
            if (
                request.url.path.startswith("/api/") or request.url.path == "/openapi.json"
            ) and not is_admin:
                return JSONResponse({"detail": "Control credential required"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; frame-ancestors 'none'; form-action 'self'"
        )
        return response

    app.add_middleware(InferenceLifetime, manager=manager)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/status")
    async def status():
        manager.refresh_liveness()
        native = await manager.native_busy()
        return {
            "selected_engine": manager.engine,
            "active_settings": manager.active_settings,
            "selection_id": manager.selection_id,
            "loaded_model": manager.model if manager.phase == "ready" else None,
            "phase": manager.phase,
            "native_busy": native,
            "busy": True
            if manager.active
            else (
                False if manager.model and manager.spec().get("probe") == "gateway_only" else native
            ),
            "occupancy_source": "gateway_only"
            if manager.model and manager.spec().get("probe") == "gateway_only"
            else "native_slots_and_gateway",
            "requests": [
                {
                    "id": key,
                    "client": value["client"],
                    "elapsed_seconds": round(time.time() - value["started"], 1),
                }
                for key, value in manager.active.items()
            ],
            "active_requests": len(manager.active),
            "error": manager.last_error,
            "native_processes": [
                {"pid": p, "start_time": s} for p, s in manager.native_processes()
            ],
        }

    @app.post("/api/engine")
    async def engine(payload: EngineSelection):
        if manager.lifecycle_busy and not (payload.force and manager.phase == "loading"):
            raise HTTPException(409, "Lifecycle operation already running")
        await manager.select_engine(payload.engine, payload.force)
        return await status()

    @app.get("/api/engines")
    async def engines():
        return {
            "unavailable_registrations": registry.unavailable_summary(),
            "engines": [
                {
                    "id": name,
                    "label": e.get("label", name),
                    "profiles": [
                        {
                            "id": key,
                            "label": profile.get("label", key),
                            "format": profile["format"],
                            "artifacts": list(profile.get("artifacts", {})),
                        }
                        for key, profile in e.get("profiles", {}).items()
                    ],
                    "models": [summary(key, spec) for key, spec in e["models"].items()],
                }
                for name, e in config["engines"].items()
            ],
        }

    @app.get("/api/model-files")
    async def model_files(root: str | None = None, path: str = ""):
        return await asyncio.to_thread(registry.files, root, path)

    @app.post("/api/models/inspect")
    async def inspect_model(payload: dict):
        candidate, spec, metadata = await asyncio.to_thread(registry.inspect, payload)
        return {
            "candidate": candidate,
            "summary": {
                "engine": candidate["engine"],
                **summary(candidate["model"], spec),
                **metadata,
            },
            "warnings": [
                "Metadata inspection does not verify runtime/model compatibility; no model was loaded"
            ],
        }

    @app.post("/api/models", status_code=201)
    async def register_model(payload: dict):
        async with registry_lock:
            worker = asyncio.create_task(asyncio.to_thread(registry.persist, payload))
            try:
                candidate, spec = await asyncio.shield(worker)
            except asyncio.CancelledError:
                # A thread cannot be cancelled mid-fsync. Keep serialization until
                # it finishes, and make every successful durable write visible.
                try:
                    candidate, spec = await finish_cleanup(worker)
                except Exception:
                    raise asyncio.CancelledError() from None
                registry._install(candidate, spec)
                raise
            registry._install(candidate, spec)
            return {"engine": candidate["engine"], **summary(candidate["model"], spec)}

    @app.delete("/api/models/{engine}/{model}")
    async def remove_model(engine: str, model: str):
        if manager.lifecycle_busy:
            raise HTTPException(409, "Lifecycle operation already running")
        async with registry_lock, manager.lock:
            if manager.engine == engine and manager.model == model:
                raise HTTPException(409, "Unload the model before removing its registration")
            worker = asyncio.create_task(asyncio.to_thread(registry.persist_remove, engine, model))
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                try:
                    await finish_cleanup(worker)
                except Exception:
                    raise asyncio.CancelledError() from None
                registry.uninstall(engine, model)
                raise
            registry.uninstall(engine, model)
            return {"engine": engine, "id": model, "removed": True}

    @app.get("/api/telemetry")
    async def metrics():
        return telemetry()

    @app.post("/api/model")
    async def model(payload: ModelSelection):
        if manager.lifecycle_busy and not (payload.force and manager.phase == "loading"):
            raise HTTPException(409, "Lifecycle operation already running")
        try:
            await manager.load_model(
                payload.model,
                payload.force,
                engine=payload.engine,
                context=payload.context,
                reasoning=payload.reasoning,
            )
        except (OSError, RuntimeError, TimeoutError):
            raise HTTPException(503, "Backend startup failed; inspect local engine log") from None
        return await status()

    @app.post("/api/unload")
    async def unload(payload: Force):
        if manager.lifecycle_busy and not (payload.force and manager.phase == "loading"):
            raise HTTPException(409, "Lifecycle operation already running")
        await manager.unload(payload.force)
        return await status()

    @app.get("/v1/models")
    async def models():
        manager.refresh_liveness()
        if manager.phase == "ready":
            await manager.native_busy()
        return {
            "object": "list",
            "data": [
                {
                    "id": key,
                    "object": "model",
                    "owned_by": name,
                    "context_length": manager.active_settings["context"]
                    if manager.engine == name
                    and manager.model == key
                    and manager.phase == "ready"
                    and manager.active_settings
                    else spec.get("context"),
                    "status": "loaded"
                    if manager.engine == name and manager.model == key and manager.phase == "ready"
                    else "unloaded",
                }
                for name, e in config["engines"].items()
                for key, spec in e["models"].items()
                if spec.get("enabled", True)
            ],
        }

    static = Path(__file__).with_name("static")
    app.mount("/assets", StaticFiles(directory=static), name="assets")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(static / "index.html")

    @app.post("/v1/{endpoint}")
    async def infer(endpoint: str, request: Request):
        if endpoint not in ("completions", "responses", "messages"):
            raise HTTPException(404, "Unsupported inference endpoint")
        return await proxy("/v1/" + endpoint, request)

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        return await proxy("/v1/chat/completions", request)

    async def proxy(path, request):
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 8 * 1024 * 1024:
                raise HTTPException(413, "Body exceeds 8 MiB")
        try:
            body = json.loads(data, parse_constant=reject_constant, parse_float=finite_float)
            if not isinstance(body, dict):
                raise ValueError()
            validate_json_depth(body)
        except (ValueError, TypeError, RecursionError):
            raise HTTPException(400, "JSON object required") from None
        request_id = request.scope["igm_request_id"]
        if manager.lifecycle_busy:
            raise HTTPException(409, "Lifecycle operation in progress; retry when ready")
        async with manager.lock:
            selection = request.headers.get("x-igm-selection-id")
            if selection is not None and selection != manager.selection_id:
                raise HTTPException(409, "Selection changed; refresh status before inference")
            if manager.phase != "ready" or body.get("model") != manager.model:
                raise HTTPException(
                    409, "Requested model is not loaded; select engine and model first"
                )
            if manager.process is None or manager.process.poll() is not None:
                manager.phase = "error"
                raise HTTPException(503, "Backend exited; reload using control API")
            spec = manager.spec()
            native = await manager.native_busy()
            if manager.active or native is True:
                raise HTTPException(409, "Engine already processing; retry when idle")
            if native is None and spec.get("probe", "slots") == "slots":
                raise HTTPException(503, "Native occupancy unknown; inference refused")
            apply_reasoning(
                body, path, spec, (manager.active_settings or {}).get("reasoning", "auto")
            )
            body["model"] = spec.get("upstream_model", manager.model)
            manager.active[request_id] = {
                "started": time.time(),
                "client": request.headers.get("x-client-name", "unnamed")[:80],
                "task": request.scope["igm_response_task"],
            }
        upstream = None
        try:
            req = manager.http.build_request(
                "POST", spec["base_url"] + path, json=body, headers={"Accept-Encoding": "identity"}
            )
            upstream = await manager.http.send(req, stream=True)
        except (ValueError, RecursionError):
            manager.active.pop(request_id, None)
            raise HTTPException(400, "Invalid JSON payload") from None
        except httpx.HTTPError:
            manager.active.pop(request_id, None)
            raise HTTPException(502, "Backend request failed") from None
        except BaseException:
            manager.active.pop(request_id, None)
            raise

        async def stream():
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()

        return StreamingResponse(
            stream(),
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "application/json"),
            headers={"X-Request-ID": request_id, "X-Accel-Buffering": "no"},
        )

    return app
