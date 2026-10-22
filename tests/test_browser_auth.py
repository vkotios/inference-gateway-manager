"""Password sessions prove revocation, persistence and transport/CSRF boundaries."""

import asyncio
import hashlib
import sqlite3
import threading
import time

import httpx
import pytest
from fastapi import HTTPException


def auth_store(tmp_path):
    from gateway.browser_auth import BrowserAuth

    auth = BrowserAuth(tmp_path, {"enabled": True, "allow_insecure_http_sessions": True})
    auth.set_password("a sufficiently long test password")
    return auth


def test_password_session_survives_restart_without_storing_bearer_secret(tmp_path):
    from gateway.browser_auth import BrowserAuth

    auth = auth_store(tmp_path)
    token, session = auth.login("a sufficiently long test password", True, "client", "Browser")
    restored = BrowserAuth(tmp_path, {"enabled": True})
    assert restored.authenticate(token)["id"] == session["id"]
    assert session["expires_at"] - session["created_at"] == 30 * 86400
    assert token.encode() not in auth.path.read_bytes()
    with sqlite3.connect(auth.path) as conn:
        stored = conn.execute("SELECT token_hash FROM sessions").fetchone()[0]
        password = conn.execute("SELECT password_hash FROM operator WHERE id=1").fetchone()[0]
    assert stored == hashlib.sha256(token.encode()).hexdigest()
    assert password.startswith("$argon2id$")
    assert auth.path.stat().st_mode & 0o777 == 0o600


def test_frequent_session_checks_do_not_write_last_seen_every_time(tmp_path, monkeypatch):
    auth = auth_store(tmp_path)
    token, session = auth.login("a sufficiently long test password", True, "client", "Browser")
    now = session["last_seen"]
    monkeypatch.setattr("gateway.browser_auth.time.time", lambda: now + 30)
    assert auth.authenticate(token)
    with sqlite3.connect(auth.path) as conn:
        assert conn.execute("SELECT last_seen FROM sessions").fetchone()[0] == now
    monkeypatch.setattr("gateway.browser_auth.time.time", lambda: now + 61)
    assert auth.authenticate(token)
    with sqlite3.connect(auth.path) as conn:
        assert conn.execute("SELECT last_seen FROM sessions").fetchone()[0] == now + 61


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/status", "/api/auth/session"])
async def test_locked_session_database_does_not_block_health(tmp_path, path):
    from gateway.app import create_app

    app = create_app(
        {
            "allowed_hosts": ["testserver"],
            "engines": {"one": {"models": {}}},
            "browser_auth": {"enabled": True, "allow_insecure_http_sessions": True},
        },
        "a" * 32,
        "b" * 32,
        tmp_path,
    )
    auth = app.state.browser_auth
    auth.set_password("a sufficiently long test password")
    token, _ = auth.login("a sufficiently long test password", True, "client", "Browser")
    locked, release, finished = threading.Event(), threading.Event(), threading.Event()

    def hold_lock():
        with sqlite3.connect(auth.path) as conn:
            conn.execute("BEGIN EXCLUSIVE")
            locked.set()
            release.wait(2)
        finished.set()

    holder = asyncio.create_task(asyncio.to_thread(hold_lock))
    pending = None
    try:
        assert await asyncio.to_thread(locked.wait, 1)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            cookies={"igm_session": token},
        ) as client:
            pending = asyncio.create_task(client.get(path))
            await asyncio.sleep(0)
            assert not finished.is_set(), "Session lookup blocked the event loop"
            assert (await client.get("/health")).status_code == 200
            assert not finished.is_set(), "Health waited for the session database lock"
            release.set()
            assert (await pending).status_code == 200
    finally:
        release.set()
        await asyncio.gather(holder, *([pending] if pending else []), return_exceptions=True)
        await app.state.manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path,lock",
    [
        ("GET", "/api/status", "EXCLUSIVE"),
        ("GET", "/api/auth/session", "EXCLUSIVE"),
        ("GET", "/api/auth/config", "EXCLUSIVE"),
        ("GET", "/api/auth/sessions", "IMMEDIATE"),
        ("DELETE", "/api/auth/sessions/current", "IMMEDIATE"),
        ("POST", "/api/auth/logout", "IMMEDIATE"),
    ],
)
async def test_session_database_timeout_returns_controlled_unavailability(
    tmp_path, monkeypatch, method, path, lock
):
    from gateway.app import create_app

    app = create_app(
        {
            "allowed_hosts": ["testserver"],
            "engines": {"one": {"models": {}}},
            "browser_auth": {"enabled": True, "allow_insecure_http_sessions": True},
        },
        "a" * 32,
        "b" * 32,
        tmp_path,
    )
    auth = app.state.browser_auth
    auth.set_password("a sufficiently long test password")
    token, record = auth.login("a sufficiently long test password", True, "client", "Browser")
    connect = sqlite3.connect
    holder = connect(auth.path)
    holder.execute("BEGIN " + lock)

    def fast_timeout(*args, **kwargs):
        kwargs["timeout"] = 0.01
        return connect(*args, **kwargs)

    monkeypatch.setattr("gateway.browser_auth.sqlite3.connect", fast_timeout)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            cookies={"igm_session": token},
        ) as client:
            response = await client.request(
                method,
                path,
                headers={"Origin": "http://testserver", "X-CSRF-Token": record["csrf_token"]},
            )
            assert response.status_code == 503
            assert "session store" in response.json()["detail"].lower()
            assert (await client.get("/health")).status_code == 200
            assert (
                await client.get("/api/status", headers={"Authorization": "Bearer " + "a" * 32})
            ).status_code == 200
    finally:
        holder.rollback()
        holder.close()
        await app.state.manager.close()


def test_password_reset_revokes_every_browser(tmp_path):
    auth = auth_store(tmp_path)
    one, _ = auth.login("a sufficiently long test password", True, "a", "Browser")
    two, _ = auth.login("a sufficiently long test password", True, "b", "Browser")
    auth.set_password("a different sufficiently long password")
    assert auth.authenticate(one) is None
    assert auth.authenticate(two) is None
    with pytest.raises(HTTPException) as e:
        auth.login("a sufficiently long test password", True, "c", "Browser")
    assert e.value.status_code == 401


def test_session_idle_and_absolute_expiry(tmp_path):
    auth = auth_store(tmp_path)
    token, _ = auth.login("a sufficiently long test password", True, "client", "Browser")
    with sqlite3.connect(auth.path) as conn:
        conn.execute("UPDATE sessions SET last_seen=?", (time.time() - 7 * 86400 - 1,))
    assert auth.authenticate(token) is None
    token, _ = auth.login("a sufficiently long test password", False, "client", "Browser")
    with sqlite3.connect(auth.path) as conn:
        conn.execute("UPDATE sessions SET expires_at=?", (time.time() - 1,))
    assert auth.authenticate(token) is None


def test_login_rate_limit_persists_across_store_instances(tmp_path):
    from gateway.browser_auth import BrowserAuth

    auth = auth_store(tmp_path)
    for _ in range(10):
        with pytest.raises(HTTPException) as e:
            auth.login("wrong", False, "same-client", "Browser")
        assert e.value.status_code == 401
    other = BrowserAuth(tmp_path, {"enabled": True})
    with pytest.raises(HTTPException) as e:
        other.login("a sufficiently long test password", False, "same-client", "Browser")
    assert e.value.status_code == 429


@pytest.mark.asyncio
async def test_global_login_throttle_preserves_sessions_and_control_key_access(tmp_path):
    from gateway.app import create_app

    app = create_app(
        {
            "allowed_hosts": ["testserver"],
            "engines": {"one": {"models": {}}},
            "browser_auth": {"enabled": True, "allow_insecure_http_sessions": True},
        },
        "a" * 32,
        "b" * 32,
        tmp_path,
    )
    auth = app.state.browser_auth
    auth.set_password("a sufficiently long test password")
    token, _ = auth.login("a sufficiently long test password", True, "client", "Browser")
    with auth.db() as conn:
        conn.executemany(
            "INSERT INTO attempts VALUES (?, ?)", [(str(i), time.time()) for i in range(60)]
        )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            cookies={"igm_session": token},
        ) as client:
            payload = {"password": "a sufficiently long test password"}
            assert (
                await client.post(
                    "/api/auth/login", json=payload, headers={"Origin": "http://testserver"}
                )
            ).status_code == 429
            assert (await client.get("/api/status")).status_code == 200
            assert (
                await client.get("/api/status", headers={"Authorization": "Bearer " + "a" * 32})
            ).status_code == 200
            with auth.db() as conn:
                conn.execute("UPDATE attempts SET at=?", (time.time() - 61,))
            assert (
                await client.post(
                    "/api/auth/login", json=payload, headers={"Origin": "http://testserver"}
                )
            ).status_code == 200
    finally:
        await app.state.manager.close()


def test_disabled_auth_and_short_password_are_refused(tmp_path):
    from gateway.browser_auth import BrowserAuth

    auth = BrowserAuth(tmp_path, {})
    with pytest.raises(ValueError):
        auth.set_password("short")
    assert not auth.enabled
    assert auth.authenticate("any") is None
    with pytest.raises(HTTPException) as e:
        auth.login("anything", False, "a", "Browser")
    assert e.value.status_code == 403


def test_individual_session_revocation_keeps_other_browser(tmp_path):
    auth = auth_store(tmp_path)
    one, session = auth.login("a sufficiently long test password", True, "a", "Browser A")
    two, _ = auth.login("a sufficiently long test password", True, "b", "Browser B")
    assert len(auth.sessions()) == 2
    auth.revoke(session["id"])
    assert auth.authenticate(one) is None
    assert auth.authenticate(two) is not None


def test_cookie_transport_and_csrf_requirements(tmp_path):
    from gateway.browser_auth import BrowserAuth

    auth = BrowserAuth(tmp_path, {"enabled": True})
    with pytest.raises(HTTPException) as e:
        auth.check_transport("http")
    assert e.value.status_code == 403
    auth.check_transport("https")
    auth = auth_store(tmp_path)
    _, session = auth.login("a sufficiently long test password", True, "a", "Browser")
    auth.check_csrf(session, session["csrf_token"], True)
    for token, origin_ok in [("wrong", True), (session["csrf_token"], False), (None, True)]:
        with pytest.raises(HTTPException) as e:
            auth.check_csrf(session, token, origin_ok)
        assert e.value.status_code == 403


@pytest.mark.asyncio
async def test_http_login_restores_control_but_never_grants_inference(tmp_path):
    from gateway.app import create_app

    config = {
        "allowed_hosts": ["testserver"],
        "engines": {"one": {"models": {}}},
        "browser_auth": {"enabled": True, "allow_insecure_http_sessions": True},
    }
    auth_store(tmp_path)
    app = create_app(config, "a" * 32, "b" * 32, tmp_path)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            result = await client.post(
                "/api/auth/login",
                headers={"Origin": "http://testserver"},
                json={"password": "a sufficiently long test password", "remember": True},
            )
            assert result.status_code == 200
            cookie = result.headers["set-cookie"]
            assert (
                "HttpOnly" in cookie and "SameSite=strict" in cookie and "Max-Age=2592000" in cookie
            )
            csrf = result.json()["csrf_token"]
            assert (await client.get("/api/auth/session")).json()["csrf_token"] == csrf
            assert (await client.get("/api/status")).status_code == 200
            assert (await client.get("/v1/models")).status_code == 401
            assert (await client.post("/api/unload", json={})).status_code == 403
            assert (
                await client.post(
                    "/api/unload",
                    json={},
                    headers={"Origin": "http://testserver", "X-CSRF-Token": csrf},
                )
            ).status_code == 200
            assert (
                await client.get("/api/status", headers={"Authorization": "Bearer invalid"})
            ).status_code == 401
            assert (
                await client.get("/api/status", headers={"Authorization": "Bearer " + "b" * 32})
            ).status_code == 403
            assert (await client.get("/api/auth/sessions")).json()["sessions"][0]["current"]
            assert (
                await client.post(
                    "/api/auth/logout",
                    headers={"Origin": "http://testserver", "X-CSRF-Token": csrf},
                )
            ).status_code == 200
            assert (await client.get("/api/status")).status_code == 401
    finally:
        await app.state.manager.close()


@pytest.mark.asyncio
async def test_http_login_requires_transport_opt_in_and_same_origin(tmp_path):
    from gateway.app import create_app

    auth_store(tmp_path)
    config = {
        "allowed_hosts": ["testserver"],
        "engines": {"one": {"models": {}}},
        "browser_auth": {"enabled": True},
    }
    app = create_app(config, "a" * 32, "b" * 32, tmp_path)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            payload = {"password": "a sufficiently long test password", "remember": True}
            result = await client.post(
                "/api/auth/login",
                json=payload,
                headers={"Origin": "http://testserver", "X-Forwarded-Proto": "https"},
            )
            assert result.status_code == 403
            config["browser_auth"]["allow_insecure_http_sessions"] = True
            for origin in [None, "http://evil.test"]:
                headers = {"Origin": origin} if origin else {}
                assert (
                    await client.post("/api/auth/login", json=payload, headers=headers)
                ).status_code == 403
            assert (
                await client.get("/api/status", headers={"Authorization": "Bearer " + "a" * 32})
            ).status_code == 200
    finally:
        await app.state.manager.close()


@pytest.mark.asyncio
async def test_https_login_issues_secure_cookie_and_api_revokes_it(tmp_path):
    from gateway.app import create_app

    auth_store(tmp_path)
    config = {
        "allowed_hosts": ["testserver"],
        "engines": {"one": {"models": {}}},
        "browser_auth": {"enabled": True},
    }
    app = create_app(config, "a" * 32, "b" * 32, tmp_path)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://testserver"
        ) as client:
            result = await client.post(
                "/api/auth/login",
                headers={"Origin": "https://testserver"},
                json={"password": "a sufficiently long test password"},
            )
            assert result.status_code == 200
            assert "Secure" in result.headers["set-cookie"]
            session_id = result.json()["session_id"]
            assert (
                await client.delete(
                    "/api/auth/sessions/" + session_id,
                    headers={"Authorization": "Bearer " + "a" * 32},
                )
            ).status_code == 200
            assert (await client.get("/api/status")).status_code == 401
    finally:
        await app.state.manager.close()


@pytest.mark.asyncio
async def test_https_public_origin_does_not_authorize_direct_lan_plaintext_login(tmp_path):
    from gateway.app import create_app

    auth_store(tmp_path)
    config = {
        "allowed_hosts": ["testserver"],
        "allowed_origins": ["https://testserver"],
        "engines": {"one": {"models": {}}},
        "browser_auth": {"enabled": True, "public_origin": "https://testserver"},
    }
    app = create_app(config, "a" * 32, "b" * 32, tmp_path)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("192.168.1.12", 1234)),
            base_url="http://testserver",
        ) as client:
            result = await client.post(
                "/api/auth/login",
                headers={"Origin": "https://testserver"},
                json={"password": "a sufficiently long test password"},
            )
            assert result.status_code == 403
    finally:
        await app.state.manager.close()
