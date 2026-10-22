"""Real Chromium workflows for the control dashboard and session boundary."""

import asyncio
import json
import socket
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import uvicorn
from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "a sufficiently long test password"


@pytest.fixture
async def live_gateway(tmp_path):
    from gateway.app import create_app
    from gateway.browser_auth import BrowserAuth

    auth = BrowserAuth(tmp_path, {"enabled": True, "allow_insecure_http_sessions": True})
    auth.set_password(PASSWORD)
    config = {
        "allowed_hosts": ["127.0.0.1"],
        "engines": {"one": {"models": {}}},
        "browser_auth": {"enabled": True, "allow_insecure_http_sessions": True},
    }
    app = create_app(config, "a" * 32, "b" * 32, tmp_path)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started
    yield f"http://127.0.0.1:{port}", auth
    server.should_exit = True
    await asyncio.to_thread(thread.join, 5)


@pytest.fixture
async def browser_page():
    async with async_playwright() as p:
        binaries = sorted(
            (Path.home() / ".cache/ms-playwright").glob("chromium-*/chrome-linux64/chrome")
        )
        executable = str(binaries[-1]) if binaries else None
        browser = await p.chromium.launch(
            headless=True, executable_path=executable, args=["--no-sandbox"]
        )
        page = await browser.new_page()
        yield page
        await browser.close()


class DashboardAPI:
    def __init__(self):
        self.authenticated = False
        self.requests = []
        self.hold_load = None
        self.hold_inspect = None
        self.roots = [{"id": "models", "label": "Local models"}]
        self.entries = [
            {"name": "sample.gguf", "path": "sample.gguf", "kind": "file"},
            {"name": "pack", "path": "pack", "kind": "directory"},
        ]
        self.errors = {}
        self.state = {
            "phase": "idle",
            "selected_engine": "one",
            "loaded_model": None,
            "active_requests": 0,
            "busy": False,
            "native_busy": False,
            "native_processes": [],
            "active_settings": {"context": 4096, "reasoning": "auto"},
        }
        self.engines = [
            {
                "id": "one",
                "label": "Fixture engine",
                "profiles": [{"id": "gguf", "label": "GGUF", "format": "gguf", "artifacts": []}],
                "models": [
                    {
                        "id": "fixture",
                        "context": 4096,
                        "enabled": True,
                        "context_limits": {
                            "min": 512,
                            "max": 8192,
                            "presets": [2048, 4096, 8192],
                            "adjustable": True,
                        },
                        "reasoning_modes": ["auto", "on", "off"],
                        "reasoning_change": "restart",
                    },
                    {
                        "id": "fixed",
                        "context": 2048,
                        "enabled": True,
                        "context_limits": {
                            "min": 2048,
                            "max": 2048,
                            "presets": [],
                            "adjustable": False,
                        },
                        "reasoning_modes": ["auto"],
                        "reasoning_change": "fixed",
                    },
                ],
            }
        ]

    async def route(self, route):
        request = route.request
        path = request.url.split("/api/", 1)[1].split("?", 1)[0]
        body = request.post_data_json if request.post_data else None
        self.requests.append((request.method, path, body, request.headers))
        status, result = 200, {}
        if path in self.errors:
            status, result = self.errors[path]
        elif path == "auth/config":
            result = {"enabled": True, "configured": True, "http_allowed": True}
        elif path == "auth/login":
            self.authenticated = body["password"] == PASSWORD
            if not self.authenticated:
                status, result = 401, {"detail": "Invalid password"}
            else:
                result = {
                    "authenticated": True,
                    "csrf_token": "csrf",
                    "session_id": "current",
                    "expires_at": 9999999999,
                }
        elif path == "auth/session":
            if self.authenticated:
                result = {
                    "authenticated": True,
                    "csrf_token": "csrf",
                    "session_id": "current",
                    "expires_at": 9999999999,
                }
            else:
                status, result = 401, {"detail": "Session expired"}
        elif not self.authenticated:
            status, result = 401, {"detail": "Session expired"}
        elif path == "auth/logout":
            self.authenticated = False
        elif path == "auth/sessions":
            result = {
                "sessions": [
                    {
                        "id": "current",
                        "label": "This browser",
                        "current": True,
                        "last_seen": 1800000000,
                        "expires_at": 1900000000,
                    },
                    {
                        "id": "other",
                        "label": "Other browser",
                        "current": False,
                        "last_seen": 1800000000,
                        "expires_at": 1900000000,
                    },
                ]
            }
        elif path.startswith("auth/sessions/"):
            if path.endswith("current"):
                self.authenticated = False
        elif path == "engines":
            result = {"engines": self.engines}
        elif path == "status":
            result = self.state.copy()
        elif path == "recovery":
            result = {"restart_gateway": True, "reboot_host": True, "pending": False}
        elif path == "telemetry":
            result = {
                "memory": {"total_bytes": 100, "available_bytes": 70, "pressure_bytes": 30},
                "gpus": [],
                "cpu_percent": 1,
                "swap_used_bytes": 0,
            }
        elif path == "model":
            self.state["phase"] = "loading"
            if self.hold_load:
                await self.hold_load.wait()
            self.state["phase"] = "idle"
            result = self.state.copy()
        elif path == "unload":
            if self.hold_load:
                self.hold_load.set()
        elif path == "model-files":
            if "root=" not in request.url:
                result = {"roots": self.roots}
            else:
                query = parse_qs(urlsplit(request.url).query)
                folder = query.get("path", [""])[0]
                result = {
                    "root": "models",
                    "path": folder,
                    "parent": "" if folder else None,
                    "absolute_path": "/models" + ("/" + folder if folder else ""),
                    "entries": self.entries,
                }
        elif path == "models/inspect":
            if self.hold_inspect:
                await self.hold_inspect.wait()
            result = {
                "candidate": {**body, "model": body.get("model") or "sample"},
                "summary": {"id": "sample", "context": 4096, "artifact_count": 1},
                "warnings": ["No model was loaded during inspection."],
            }
        elif path == "models":
            status, result = 201, {"id": body["model"]}
            self.engines[0]["models"].append(
                {"id": body["model"], "enabled": True, "context": 4096}
            )
        await route.fulfill(status=status, content_type="application/json", body=json.dumps(result))


async def mocked_dashboard(page, url):
    api = DashboardAPI()
    await page.route("**/api/**", api.route)
    await page.goto(url)
    return api


async def login(page):
    await page.get_by_label("Password", exact=True).fill(PASSWORD)
    await page.get_by_role("button", name="Sign in", exact=True).click()
    await expect(page.locator("#connection")).to_have_text("CONNECTED")


async def test_real_password_cookie_restore_logout(browser_page, live_gateway):
    page = browser_page
    url, _ = live_gateway
    await page.goto(url)
    await page.get_by_label("Password", exact=True).fill("wrong")
    await page.get_by_role("button", name="Sign in", exact=True).click()
    await expect(page.locator("#auth-error")).not_to_be_empty()
    await page.get_by_label("Remember this browser").check()
    await login(page)
    cookies = await page.context.cookies()
    assert any(c["httpOnly"] and c["expires"] > 0 for c in cookies)
    await page.reload()
    await expect(page.locator("#connection")).to_have_text("CONNECTED")
    await page.get_by_role("button", name="Sign out", exact=True).click()
    await expect(page.locator("#connection")).to_have_text("DISCONNECTED")
    await expect(page.locator("#engine")).to_be_disabled()


async def test_capabilities_payload_and_expiry(browser_page, live_gateway):
    page = browser_page
    api = await mocked_dashboard(page, live_gateway[0])
    await login(page)
    await page.locator("#model").select_option("fixture")
    await page.get_by_label("Context capacity", exact=True).select_option("8192")
    await page.get_by_label("Default reasoning", exact=True).select_option("off")
    await page.get_by_role("button", name="Load model", exact=True).click()
    await expect(page.locator("#notice")).to_have_text("Lifecycle action completed.")
    payload = next(r for r in api.requests if r[1] == "model")
    assert payload[2] == {
        "model": "fixture",
        "engine": "one",
        "context": 8192,
        "reasoning": "off",
        "force": False,
    }
    assert payload[3]["x-csrf-token"] == "csrf"
    assert "authorization" not in payload[3]
    await page.locator("#model").select_option("fixed")
    await expect(page.locator("#context")).to_be_disabled()
    await expect(page.locator("#reasoning")).to_be_disabled()
    api.authenticated = False
    await page.evaluate("poll()")
    await expect(page.locator("#connection")).to_have_text("DISCONNECTED")
    await expect(page.locator("#loaded")).to_have_text("Nothing loaded")
    await expect(page.locator("#engine")).to_be_disabled()


async def test_registration_browse_review_save_and_stale(browser_page, live_gateway):
    page = browser_page
    api = await mocked_dashboard(page, live_gateway[0])
    await login(page)
    await page.get_by_text("Register a local model", exact=True).click()
    await page.get_by_role("button", name="Browse model path", exact=True).click()
    await expect(page.get_by_role("dialog", name="Browse server models")).to_be_visible()
    await page.get_by_role("button", name="Choose sample.gguf", exact=True).click()
    await expect(page.locator("#register-path")).to_have_value("/models/sample.gguf")
    await page.get_by_role("button", name="Inspect model", exact=True).click()
    await expect(page.locator("#add-model")).to_be_enabled()
    await page.screenshot(path="/tmp/gateway-dashboard-desktop.png", full_page=True)
    await page.get_by_label("Model ID", exact=True).fill("changed")
    await expect(page.locator("#add-model")).to_be_disabled()
    await page.get_by_role("button", name="Inspect model", exact=True).click()
    await expect(page.locator("#add-model")).to_be_enabled()
    await page.get_by_role("button", name="Add model", exact=True).click()
    await expect(page.locator("#model")).to_have_value("changed")
    assert next(r for r in api.requests if r[1] == "models")[2]["model"] == "changed"
    assert not any(r[1] == "model" for r in api.requests)
    await page.get_by_role("button", name="Browse model path", exact=True).click()
    await page.get_by_role("button", name="Choose sample.gguf", exact=True).click()
    api.hold_inspect = asyncio.Event()
    await page.get_by_role("button", name="Inspect model", exact=True).click()
    await page.get_by_label("Model ID", exact=True).fill("newer")
    async with page.expect_response("**/api/models/inspect"):
        api.hold_inspect.set()
    await page.wait_for_function("!inspecting")
    await expect(page.locator("#add-model")).to_be_disabled()


async def test_force_cancel_loading_recovery_and_keyboard(browser_page, live_gateway):
    page = browser_page
    api = await mocked_dashboard(page, live_gateway[0])
    await login(page)
    api.hold_load = asyncio.Event()
    await page.locator("#model").select_option("fixture")
    await page.get_by_role("button", name="Load model", exact=True).click()
    await page.evaluate("poll()")
    await page.get_by_text("Interrupt an active request", exact=True).click()
    await page.locator("#force").check()
    await page.locator("#unload").click()
    dialog = page.get_by_role("dialog", name="Interrupt current inference?")
    await expect(dialog).to_be_visible()
    await expect(dialog.get_by_role("button", name="Cancel", exact=True)).to_be_focused()
    await page.keyboard.press("Escape")
    assert not any(r[1] == "unload" for r in api.requests)
    await page.locator("#unload").click()
    await dialog.get_by_role("button", name="Interrupt inference", exact=True).click()
    await expect(page.locator("#force")).not_to_be_checked()
    assert any(r[1] == "unload" and r[2]["force"] for r in api.requests)
    await page.get_by_role("button", name="Restart gateway", exact=True).click()
    await page.keyboard.press("Escape")
    assert not any(r[1].startswith("recovery/") for r in api.requests)
    await page.set_viewport_size({"width": 390, "height": 844})
    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    await page.locator("#model").focus()
    await page.keyboard.press("ArrowDown")
    await page.keyboard.press("Enter")
    await page.screenshot(path="/tmp/gateway-dashboard-narrow.png", full_page=True)


async def test_custom_context_validation_and_catalog_refresh(browser_page, live_gateway):
    page = browser_page
    api = await mocked_dashboard(page, live_gateway[0])
    await login(page)
    await page.locator("#model").select_option("fixture")
    await page.get_by_label("Context capacity", exact=True).select_option("custom")
    custom = page.get_by_label("Custom context capacity", exact=True)
    await custom.fill("8193")
    await page.get_by_role("button", name="Load model", exact=True).click()
    await expect(custom).to_have_attribute("aria-invalid", "true")
    assert not any(r[1] == "model" for r in api.requests)
    await custom.fill("6144")
    await page.get_by_label("Default reasoning", exact=True).select_option("on")
    await page.evaluate("poll()")
    await expect(custom).to_have_value("6144")
    api.engines[0]["models"].append({"id": "external", "context": 4096, "enabled": True})
    await page.get_by_role("button", name="Refresh models", exact=True).click()
    await expect(page.locator("#model option[value=external]")).to_have_count(1)
    await expect(page.locator("#context")).to_have_value("custom")
    await expect(custom).to_have_value("6144")
    await expect(page.locator("#reasoning")).to_have_value("on")
    await page.get_by_role("button", name="Load model", exact=True).click()
    await expect(page.locator("#notice")).to_have_text("Lifecycle action completed.")
    assert next(r for r in api.requests if r[1] == "model")[2]["context"] == 6144


async def test_real_session_revocation_and_keyboard(browser_page, live_gateway):
    page = browser_page
    url, auth = live_gateway
    await page.goto(url)
    await login(page)
    token, other = auth.login(PASSWORD, False, "other", "Other browser")
    await page.get_by_role("button", name="Browser sessions", exact=True).click()
    row = page.locator(".session-row").filter(has_text="Other browser")
    await row.get_by_role("button", name="Revoke browser", exact=True).click()
    dialog = page.get_by_role("dialog", name="Revoke browser access?")
    await expect(dialog.get_by_role("button", name="Cancel", exact=True)).to_be_focused()
    await page.keyboard.press("Escape")
    assert auth.authenticate(token) is not None
    await row.get_by_role("button", name="Revoke browser", exact=True).click()
    await dialog.get_by_role("button", name="Revoke access", exact=True).click()
    await expect(page.locator("#notice")).to_have_text("Browser access revoked.")
    assert auth.authenticate(token) is None
    await page.get_by_role("button", name="Revoke this browser", exact=True).click()
    await dialog.get_by_role("button", name="Revoke access", exact=True).click()
    await expect(page.locator("#connection")).to_have_text("DISCONNECTED")
    await expect(page.locator("#engine")).to_be_disabled()


async def test_directory_companions_filter_pagination_empty_and_errors(browser_page, live_gateway):
    page = browser_page
    api = await mocked_dashboard(page, live_gateway[0])
    api.engines[0]["profiles"].append(
        {"id": "pack", "label": "Model pack", "format": "strata", "artifacts": ["vision"]}
    )
    await login(page)
    await page.get_by_text("Register a local model", exact=True).click()
    await page.locator("#register-profile").select_option("pack")
    await page.get_by_role("button", name="Browse model path", exact=True).click()
    await page.get_by_role("button", name="Choose this directory", exact=True).click()
    await expect(page.locator("#register-path")).to_have_value("/models")
    await page.get_by_role("button", name="Browse companion vision", exact=True).click()
    await page.get_by_label("Filter this folder", exact=True).fill("not-present")
    await expect(page.locator("#file-status")).to_contain_text("No matching entries")
    await page.get_by_role("button", name="Clear file filter", exact=True).click()
    await expect(page.get_by_label("Filter this folder", exact=True)).to_be_focused()
    await page.get_by_role("button", name="Choose this directory", exact=True).click()
    await page.get_by_role("button", name="Inspect model", exact=True).click()
    await expect(page.locator("#add-model")).to_be_enabled()
    payload = next(r for r in api.requests if r[1] == "models/inspect")[2]
    assert payload["path"] == "/models" and payload["artifacts"] == {"vision": "/models"}
    api.entries = [
        {"name": f"model-{i}.gguf", "path": f"model-{i}.gguf", "kind": "file"} for i in range(120)
    ]
    await page.get_by_role("button", name="Browse model path", exact=True).click()
    await expect(page.locator(".file-row")).to_have_count(100)
    await page.get_by_role("button", name="Show more entries", exact=True).click()
    await expect(page.locator(".file-row")).to_have_count(120)
    await page.get_by_role("button", name="Choose model-0.gguf", exact=True).click()
    api.errors["models/inspect"] = (422, {"detail": "Unsupported model metadata"})
    await page.get_by_role("button", name="Inspect model", exact=True).click()
    await expect(page.locator("#registration-status")).to_contain_text("Unsupported model metadata")
    await expect(page.locator("#add-model")).to_be_disabled()
    api.roots = []
    await page.get_by_role("button", name="Browse model path", exact=True).click()
    await expect(page.locator("#file-status")).to_contain_text("No approved model roots")
    await page.keyboard.press("Escape")
    api.engines[0]["profiles"] = []
    await page.get_by_role("button", name="Refresh models", exact=True).click()
    await expect(page.locator("#registration-help")).to_contain_text("No registration profiles")


async def test_control_key_and_csrf_reauthentication(browser_page, live_gateway):
    page = browser_page
    url, _ = live_gateway
    await page.goto(url)
    await page.get_by_text("Advanced: control key", exact=True).click()
    await page.get_by_label("Control credential", exact=True).fill("a" * 32)
    await page.get_by_role("button", name="Show control key", exact=True).click()
    await expect(page.locator("#key")).to_have_attribute("type", "text")
    await page.get_by_role("button", name="Connect", exact=True).click()
    await expect(page.locator("#connection")).to_have_text("CONNECTED")
    await expect(page.locator("#key")).to_have_value("")
    await page.get_by_role("button", name="Forget key", exact=True).click()
    await expect(page.locator("#connection")).to_have_text("DISCONNECTED")
    await page.reload()
    await expect(page.locator("#engine")).to_be_disabled()
    api = await mocked_dashboard(page, url)
    await login(page)
    await page.locator("#model").select_option("fixture")
    api.errors["model"] = (403, {"detail": "CSRF token mismatch"})
    await page.get_by_role("button", name="Load model", exact=True).click()
    await expect(page.locator("#connection")).to_have_text("DISCONNECTED")
    await expect(page.locator("#notice")).to_contain_text("Sign in again")
    await expect(page.locator("#engine")).to_be_disabled()


async def test_saved_and_loaded_reasoning_defaults_are_preserved(browser_page, live_gateway):
    page = browser_page
    api = await mocked_dashboard(page, live_gateway[0])
    api.engines[0]["models"][0]["reasoning"] = "off"
    await login(page)
    await page.locator("#model").select_option("fixture")
    await expect(page.locator("#reasoning")).to_have_value("off")
    await page.get_by_role("button", name="Load model", exact=True).click()
    await expect(page.locator("#notice")).to_have_text("Lifecycle action completed.")
    assert next(r for r in api.requests if r[1] == "model")[2]["reasoning"] == "off"
    api.engines[0]["models"][0]["reasoning"] = "auto"
    api.state["loaded_model"] = "fixture"
    api.state["active_settings"]["reasoning"] = "off"
    api.state["active_settings"]["requested_context"] = 6144
    api.state["active_settings"]["context"] = 4096
    api.state["active_settings"]["context_source"] = "native_slots"
    await page.reload()
    await expect(page.locator("#model")).to_have_value("fixture")
    await expect(page.locator("#reasoning")).to_have_value("off")
    await expect(page.locator("#context")).to_have_value("custom")
    await expect(page.locator("#custom-context")).to_have_value("6144")
    await expect(page.locator("#active-settings")).to_contain_text("4096 tokens (native_slots)")
    await page.locator("#reasoning").select_option("on")
    await page.evaluate("poll()")
    await expect(page.locator("#reasoning")).to_have_value("on")
    await page.get_by_role("button", name="Refresh models", exact=True).click()
    await expect(page.locator("#reasoning")).to_have_value("on")
    await expect(page.locator("#context")).to_have_value("custom")
    await expect(page.locator("#custom-context")).to_have_value("6144")
    await page.get_by_role("button", name="Load model", exact=True).click()
    await expect(page.locator("#notice")).to_have_text("Lifecycle action completed.")
    payload = [r for r in api.requests if r[1] == "model"][-1][2]
    assert payload["context"] == 6144 and payload["reasoning"] == "on"


async def test_generation_line_renders_after_sign_in(browser_page, live_gateway):
    page = browser_page
    await page.goto(live_gateway[0])
    await login(page)
    await expect(page.locator("#generation")).to_have_text(
        "No generation data since the gateway started"
    )
