"""Single-operator password authentication and revocable browser sessions.

Sessions are control credentials only. API bearer roles remain in app.py; this module
never supplies a cookie credential to inference requests.
"""

import asyncio
import hashlib
import hmac
import ipaddress
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool

COOKIE = "igm_session"
PASSWORD = PasswordHasher()  # Argon2id with the library's current RFC 9106 low-memory profile.


async def session_io(operation, *args):
    """Keep disk waits off the API loop and expose no database diagnostics to clients."""
    try:
        return await asyncio.to_thread(operation, *args)
    except sqlite3.Error:
        raise HTTPException(
            503, "Browser session store unavailable; retry or use a control key"
        ) from None


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(min_length=1, max_length=1024, strict=True)
    remember: StrictBool = False


class BrowserAuth:
    def __init__(self, state_dir, config):
        self.config = config
        self.enabled = config.get("enabled", False) is True
        self.path = Path(state_dir) / "browser-auth.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        with self.db() as conn:
            conn.executescript(
                "CREATE TABLE IF NOT EXISTS operator (id INTEGER PRIMARY KEY, password_hash TEXT);"
                "CREATE TABLE IF NOT EXISTS sessions ("
                "id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, csrf_token TEXT NOT NULL,"
                "created_at REAL NOT NULL, last_seen REAL NOT NULL, expires_at REAL NOT NULL,"
                "remember INTEGER NOT NULL, label TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS attempts (source TEXT NOT NULL, at REAL NOT NULL);"
                "CREATE INDEX IF NOT EXISTS attempts_time ON attempts(at);"
            )

    @contextmanager
    def db(self):
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def configured(self):
        with self.db() as conn:
            return conn.execute("SELECT 1 FROM operator WHERE id=1").fetchone() is not None

    def set_password(self, password):
        if not isinstance(password, str) or not 12 <= len(password) <= 1024:
            raise ValueError("Use a password between 12 and 1024 characters")
        encoded = PASSWORD.hash(password)
        with self.db() as conn:
            conn.execute(
                "INSERT INTO operator VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET password_hash=excluded.password_hash",
                (encoded,),
            )
            conn.execute("DELETE FROM sessions")
            conn.execute("DELETE FROM attempts")

    def check_transport(self, scheme):
        if scheme != "https" and self.config.get("allow_insecure_http_sessions") is not True:
            raise HTTPException(403, "Browser login requires HTTPS or explicit LAN HTTP opt-in")

    def scheme(self, request):
        # TLS termination is supported only through a same-host proxy. A direct
        # LAN caller cannot claim the configured HTTPS origin as its transport.
        origin = self.config.get("public_origin")
        try:
            local = request.client and ipaddress.ip_address(request.client.host).is_loopback
        except ValueError:
            local = False
        return urlsplit(origin).scheme if origin and local else request.url.scheme

    def login(self, password, remember, source, label):
        if not self.enabled:
            raise HTTPException(403, "Password login is disabled")
        now = time.time()
        source = hashlib.sha256(source.encode()).hexdigest()
        with self.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM attempts WHERE at < ?", (now - 60,))
            total = conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
            local = conn.execute(
                "SELECT COUNT(*) FROM attempts WHERE source=?", (source,)
            ).fetchone()[0]
            if local >= 10 or total >= 60:
                raise HTTPException(429, "Too many login attempts; try again in a minute")
            conn.execute("INSERT INTO attempts VALUES (?, ?)", (source, now))
            row = conn.execute("SELECT password_hash FROM operator WHERE id=1").fetchone()
        try:
            if row is None or not PASSWORD.verify(row[0], password):
                raise HTTPException(401, "Invalid login")
        except (VerificationError, InvalidHashError):
            raise HTTPException(401, "Invalid login") from None
        token = secrets.token_urlsafe(48)
        session = {
            "id": secrets.token_hex(16),
            "csrf_token": secrets.token_urlsafe(32),
            "created_at": now,
            "last_seen": now,
            "expires_at": now + (30 * 86400 if remember else 12 * 3600),
            "remember": bool(remember),
            "label": label[:120],
        }
        with self.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # Password resets racing a costly hash must not issue a session for the old password.
            current = conn.execute("SELECT password_hash FROM operator WHERE id=1").fetchone()
            if not current or current[0] != row[0]:
                raise HTTPException(401, "Invalid login")
            conn.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session["id"],
                    hashlib.sha256(token.encode()).hexdigest(),
                    session["csrf_token"],
                    now,
                    now,
                    session["expires_at"],
                    int(remember),
                    session["label"],
                ),
            )
        return token, session

    def authenticate(self, token):
        if not self.enabled or not isinstance(token, str) or not 32 <= len(token) <= 128:
            return None
        hashed = hashlib.sha256(token.encode()).hexdigest()
        now = time.time()
        with self.db() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE token_hash=?", (hashed,)).fetchone()
            if row is None:
                return None
            idle = 7 * 86400 if row["remember"] else 12 * 3600
            if now >= row["expires_at"] or now - row["last_seen"] >= idle:
                conn.execute("DELETE FROM sessions WHERE id=?", (row["id"],))
                return None
            if now - row["last_seen"] >= 60:
                conn.execute(
                    "UPDATE sessions SET last_seen=MAX(last_seen, ?) WHERE id=? AND last_seen <= ?",
                    (now, row["id"], now - 60),
                )
        return {k: row[k] for k in row.keys() if k != "token_hash"}

    @staticmethod
    def check_csrf(session, token, origin_ok):
        if (
            not origin_ok
            or not isinstance(token, str)
            or not hmac.compare_digest(token.encode(), session["csrf_token"].encode())
        ):
            raise HTTPException(403, "Session request requires same-origin CSRF protection")

    def sessions(self):
        now = time.time()
        with self.db() as conn:
            conn.execute(
                "DELETE FROM sessions WHERE expires_at <= ? OR "
                "last_seen <= ? - CASE WHEN remember THEN 604800 ELSE 43200 END",
                (now, now),
            )
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT id, created_at, last_seen, expires_at, label FROM sessions "
                    "ORDER BY created_at DESC"
                )
            ]

    def revoke(self, session_id):
        with self.db() as conn:
            conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))


def origin_allowed(request, config):
    origin = request.headers.get("origin")
    same = f"{request.url.scheme}://{request.headers.get('host')}"
    return bool(origin and (origin == same or origin in config.get("allowed_origins", [])))


def install_routes(app, auth, config):
    """Network/Host checks remain in the app middleware, including for public auth routes."""
    slots = asyncio.Semaphore(2)

    @app.get("/api/auth/config")
    async def auth_config():
        return {
            "enabled": auth.enabled,
            "configured": auth.enabled and await session_io(auth.configured),
            "http_allowed": auth.config.get("allow_insecure_http_sessions") is True,
        }

    @app.post("/api/auth/login")
    async def login(payload: Login, request: Request):
        auth.check_transport(auth.scheme(request))
        if not origin_allowed(request, config):
            raise HTTPException(403, "Same-origin login required")
        if slots.locked():
            raise HTTPException(429, "Login busy; try again shortly")
        async with slots:
            token, session = await session_io(
                auth.login,
                payload.password,
                payload.remember,
                request.client.host if request.client else "unknown",
                request.headers.get("user-agent", "Browser"),
            )
        response = JSONResponse(
            {
                "authenticated": True,
                "csrf_token": session["csrf_token"],
                "expires_at": session["expires_at"],
                "session_id": session["id"],
            }
        )
        response.set_cookie(
            COOKIE,
            token,
            max_age=30 * 86400 if payload.remember else None,
            httponly=True,
            secure=auth.scheme(request) == "https",
            samesite="strict",
            path="/",
        )
        return response

    @app.get("/api/auth/session")
    async def session(request: Request):
        auth.check_transport(auth.scheme(request))
        record = await session_io(auth.authenticate, request.cookies.get(COOKIE))
        if not record:
            raise HTTPException(401, "Sign in to continue")
        return {
            "authenticated": True,
            "csrf_token": record["csrf_token"],
            "expires_at": record["expires_at"],
            "session_id": record["id"],
        }

    @app.post("/api/auth/logout")
    async def logout(request: Request):
        record = getattr(request.state, "browser_session", None)
        if record:
            await session_io(auth.revoke, record["id"])
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(
            COOKIE,
            path="/",
            httponly=True,
            samesite="strict",
            secure=auth.scheme(request) == "https",
        )
        return response

    @app.get("/api/auth/sessions")
    async def sessions(request: Request):
        current = getattr(request.state, "browser_session", None)
        return {
            "sessions": [
                {**s, "current": bool(current and s["id"] == current["id"])}
                for s in await session_io(auth.sessions)
            ]
        }

    @app.delete("/api/auth/sessions/{session_id}")
    async def revoke(session_id: str):
        await session_io(auth.revoke, session_id)
        return {"revoked": True}
